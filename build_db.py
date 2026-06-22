"""
从零构建 WoW 制造业配方数据库。
数据来源：
  - Blizzard API：职业列表、技能层、配方名（每个职业取最新技能层）
  - Wowhead：完整材料清单（Blizzard /recipe/{id} 对新版本配方 crafted_item 全为 null）

运行一次即可，配方数据不会频繁变化。
"""

import sys, io, sqlite3, requests, re, time, os, datetime

from dotenv import load_dotenv
load_dotenv()
CLIENT_ID     = os.environ["BNET_CLIENT_ID"]
CLIENT_SECRET = os.environ["BNET_CLIENT_SECRET"]
API_BASE      = "https://us.api.blizzard.com"
TOKEN_URL     = "https://oauth.battle.net/token"
WH_HEADERS    = {"User-Agent": "Mozilla/5.0"}
DB_FILE       = "wow_recipes.db"

# 制造业职业 ID（跳过草药/采矿/剥皮/钓鱼等纯采集职业）
CRAFTING_PROF_IDS = [164, 165, 171, 185, 197, 202, 333, 755, 773]


# ──────────────────────────────────────────────
# Blizzard API
# ──────────────────────────────────────────────

def get_token():
    r = requests.post(TOKEN_URL,
                      data={"grant_type": "client_credentials"},
                      auth=(CLIENT_ID, CLIENT_SECRET), timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]



def get_all_skill_tiers(token, prof_id):
    """
    返回该职业所有技能层列表和职业名称。
    返回 ([(tier_id, tier_name), ...], prof_name_en, prof_name_zh)。
    """
    def fetch(locale):
        r = requests.get(f"{API_BASE}/data/wow/profession/{prof_id}",
                         params={"namespace": "static-us", "locale": locale},
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        return r.json()

    en = fetch("en_US")
    zh = fetch("zh_CN")

    tiers = [(t["id"], t["name"]) for t in en.get("skill_tiers", [])]
    return tiers, en["name"], zh["name"]


def get_recipes_in_tier(token, prof_id, tier_id):
    """返回 [(blizz_recipe_id, name_en, name_zh, category_en)]"""
    def fetch(locale):
        r = requests.get(f"{API_BASE}/data/wow/profession/{prof_id}/skill-tier/{tier_id}",
                         params={"namespace": "static-us", "locale": locale},
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        return r.json()

    en = fetch("en_US")
    zh = fetch("zh_CN")

    # 建 blizz_id -> zh_name 映射
    zh_map = {}
    for cat in zh.get("categories", []):
        for rec in cat.get("recipes", []):
            zh_map[rec["id"]] = rec["name"]

    rows = []
    for cat in en.get("categories", []):
        for rec in cat.get("recipes", []):
            rows.append((rec["id"], rec["name"], zh_map.get(rec["id"]), cat["name"]))
    return rows


# ──────────────────────────────────────────────
# Wowhead
# ──────────────────────────────────────────────

def wh_request(url, params=None, retries=4):
    """带重试的 Wowhead 请求，遇到 429/403 时指数退避。"""
    delay = 2
    for attempt in range(retries):
        r = requests.get(url, params=params, headers=WH_HEADERS, timeout=15)
        if r.status_code == 200:
            return r
        if r.status_code in (429, 403):
            wait = delay * (2 ** attempt)
            print(f"    [限流] {r.status_code}，等待 {wait}s 后重试...")
            time.sleep(wait)
        else:
            r.raise_for_status()
    raise Exception(f"Wowhead 请求失败，已重试 {retries} 次：{url}")


def wh_search(name):
    """
    搜 Wowhead，返回 [(spell_id, item_id)]。
    spell_id 来自 sourcemore[].ti 字段，是配方对应的 Wowhead spell ID。
    """
    r = wh_request("https://www.wowhead.com/search", params={"q": name, "json": 1})
    results = []
    for match in re.finditer(
        r'"id":(\d+),[^}]*"sourcemore":\[\{"c":\d+[^}]*"ti":(\d+)', r.text
    ):
        item_id  = int(match.group(1))
        spell_id = int(match.group(2))
        results.append((spell_id, item_id))
    return results


def wh_tooltip_html(spell_id):
    """从 nether.wowhead.com 拿配方 tooltip HTML，失败返回空字符串。"""
    r = wh_request(f"https://nether.wowhead.com/tooltip/spell/{spell_id}")
    return r.json().get("tooltip", "")


def parse_reagents(html):
    """
    从 tooltip HTML 解析必需材料（Reagents 区块，不含 Optional Reagents）。
    返回 [(item_id, quantity)]。
    """
    section_match = re.search(
        r'Reagents:<br\s*/>(.*?)(?:Optional Reagents:|<br\s*/>Creates|Creates\s*<br|$)',
        html, re.DOTALL
    )
    if not section_match:
        return []
    section = section_match.group(1)
    return [
        (int(item_match.group(1)), int(item_match.group(2)) if item_match.group(2) else 1)
        for item_match in re.finditer(
            r'/item=(\d+)/[^"]*"[^>]*>[^<]+</a>(?:&nbsp;\((\d+)\))?', section
        )
    ]


def parse_output(html):
    """
    从 tooltip HTML 解析产出物 item ID 列表和数量。
    返回 (crafted_item_ids_list, output_qty)。
    注意：tooltip-multiskill-icon 前的数字是品质评级星数，不是产出数量，不能用。
    """
    after_opts = re.split(r'Optional Reagents:', html, maxsplit=1)
    search_in = after_opts[1] if len(after_opts) > 1 else html
    crafted_ids = [
        int(m.group(1))
        for m in re.finditer(r'<span class="q\d+"><a href="/item=(\d+)/', search_in)
    ]
    return crafted_ids, 1


# ──────────────────────────────────────────────
# SQLite
# ──────────────────────────────────────────────

def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS professions (
            id      INTEGER PRIMARY KEY,
            name    TEXT NOT NULL,
            name_zh TEXT
        );

        CREATE TABLE IF NOT EXISTS skill_tiers (
            id            INTEGER PRIMARY KEY,
            profession_id INTEGER NOT NULL REFERENCES professions(id),
            name          TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipes (
            spell_id        INTEGER PRIMARY KEY,
            blizz_recipe_id INTEGER,
            profession_id   INTEGER NOT NULL REFERENCES professions(id),
            skill_tier_id   INTEGER NOT NULL REFERENCES skill_tiers(id),
            category        TEXT,
            name            TEXT NOT NULL,
            name_zh         TEXT,
            crafted_item_id INTEGER,
            output_qty      INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS recipe_reagents (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            spell_id INTEGER NOT NULL REFERENCES recipes(spell_id),
            item_id  INTEGER NOT NULL,
            quantity INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_outputs (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            spell_id INTEGER NOT NULL REFERENCES recipes(spell_id),
            item_id  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS completed_tiers (
            tier_id       INTEGER PRIMARY KEY,
            profession_id INTEGER NOT NULL,
            finished_at   TEXT NOT NULL
        );
    """)
    conn.commit()


def upsert_recipe(conn, spell_id, blizz_id, prof_id, tier_id,
                  category, name, name_zh, crafted_ids, output_qty, reagents):
    primary_id = crafted_ids[0] if crafted_ids else None
    conn.execute("""
        INSERT OR REPLACE INTO recipes
            (spell_id, blizz_recipe_id, profession_id, skill_tier_id,
             category, name, name_zh, crafted_item_id, output_qty)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (spell_id, blizz_id, prof_id, tier_id, category, name, name_zh, primary_id, output_qty))

    output_set = set(crafted_ids)
    clean_reagents = [(iid, qty) for iid, qty in reagents if iid not in output_set]

    conn.execute("DELETE FROM recipe_reagents WHERE spell_id=?", (spell_id,))
    conn.executemany(
        "INSERT INTO recipe_reagents (spell_id, item_id, quantity) VALUES (?,?,?)",
        [(spell_id, iid, qty) for iid, qty in clean_reagents]
    )

    conn.execute("DELETE FROM recipe_outputs WHERE spell_id=?", (spell_id,))
    conn.executemany(
        "INSERT INTO recipe_outputs (spell_id, item_id) VALUES (?,?)",
        [(spell_id, iid) for iid in crafted_ids]
    )
    conn.commit()


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

def build(token, conn, prof_ids):
    total_ok = total_skip = 0

    for prof_id in prof_ids:
        tiers, prof_name, prof_name_zh = get_all_skill_tiers(token, prof_id)
        if not tiers:
            print(f"  [跳过] profession {prof_id}：无技能层")
            continue

        conn.execute("INSERT OR IGNORE INTO professions (id, name, name_zh) VALUES (?,?,?)",
                     (prof_id, prof_name, prof_name_zh))
        conn.commit()

        # 每个职业共享 seen_spells，避免不同版本同一配方重复抓
        seen_spells = set()

        done_tiers = {row[0] for row in conn.execute(
            "SELECT tier_id FROM completed_tiers WHERE profession_id=?", (prof_id,)
        ).fetchall()}

        for tier_id, tier_name in tiers:
            conn.execute("INSERT OR IGNORE INTO skill_tiers (id, profession_id, name) VALUES (?,?,?)",
                         (tier_id, prof_id, tier_name))
            conn.commit()

            if tier_id in done_tiers:
                print(f"\n[{prof_name}] {tier_name}  (tier_id={tier_id}) — 已完成，跳过")
                # 已完成的 tier 的 spell_id 也要加入 seen_spells，避免后续 tier 重复插入
                for (sid,) in conn.execute(
                    "SELECT spell_id FROM recipes WHERE skill_tier_id=?", (tier_id,)
                ).fetchall():
                    seen_spells.add(sid)
                continue

            print(f"\n[{prof_name}] {tier_name}  (tier_id={tier_id})")

            try:
                recipes = get_recipes_in_tier(token, prof_id, tier_id)
            except Exception as e:
                print(f"  拉取配方列表失败: {e}")
                continue

            for blizz_id, name, name_zh, category in recipes:
                try:
                    candidates = wh_search(name)
                    inserted = False

                    for spell_id, item_id in candidates:
                        if spell_id in seen_spells:
                            continue

                        html = wh_tooltip_html(spell_id)
                        if not html:
                            continue

                        reagents = parse_reagents(html)
                        if not reagents:
                            continue

                        crafted_ids, output_qty = parse_output(html)
                        if not crafted_ids:
                            crafted_ids = [item_id]
                        upsert_recipe(conn, spell_id, blizz_id, prof_id, tier_id,
                                      category, name, name_zh, crafted_ids, output_qty, reagents)
                        seen_spells.add(spell_id)
                        inserted = True
                        total_ok += 1
                        print(f"  [OK] {name} / {name_zh}  产出 {crafted_ids} x{output_qty}  材料 {len(reagents)} 种")

                    if not inserted:
                        total_skip += 1
                        print(f"  [--] {name}  未找到 Wowhead 数据")

                    time.sleep(0.5)

                except Exception as e:
                    print(f"  [ERR] {name}: {e}")
                    total_skip += 1

            # tier 全部处理完，标记已完成
            conn.execute(
                "INSERT OR REPLACE INTO completed_tiers (tier_id, profession_id, finished_at) VALUES (?,?,?)",
                (tier_id, prof_id, datetime.datetime.now().isoformat())
            )
            conn.commit()
            print(f"  [√] {tier_name} 完成并已记录")

    print(f"\n入库：{total_ok} 个配方，跳过：{total_skip} 个")


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    # 可通过命令行指定职业 ID，否则跑全部
    # 例：python build_db.py 171        → 只跑炼金
    # 例：python build_db.py 171 333    → 炼金+附魔
    # 例：python build_db.py            → 全部制造业
    if len(sys.argv) > 1:
        prof_ids = [int(x) for x in sys.argv[1:]]
        print(f"[*] 只处理职业：{prof_ids}")
    else:
        prof_ids = CRAFTING_PROF_IDS

    token = get_token()
    print(f"[OK] Token 获取成功")

    conn = sqlite3.connect(DB_FILE)
    init_db(conn)
    print(f"[OK] 数据库初始化完成：{DB_FILE}")

    # 清理存量脏数据：产出 item 混入材料
    dirty = conn.execute("""
        SELECT rr.spell_id, rr.item_id FROM recipe_reagents rr
        JOIN recipe_outputs ro ON ro.spell_id = rr.spell_id AND ro.item_id = rr.item_id
    """).fetchall()
    if dirty:
        for spell_id, item_id in dirty:
            conn.execute("DELETE FROM recipe_reagents WHERE spell_id=? AND item_id=?", (spell_id, item_id))
        conn.commit()
        print(f"[FIX] 清除产出混入材料的脏数据 {len(dirty)} 条")

    build(token, conn, prof_ids)

    conn.close()
    print(f"\n[OK] 完成，数据库已保存：{DB_FILE}")


if __name__ == "__main__":
    main()
