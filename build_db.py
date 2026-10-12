"""
从零构建 WoW 制造业配方数据库。

数据来源（按可靠性）：
  - Blizzard API：职业 / 技能层 / 配方列表（中英文名、分类）
  - Blizzard /recipe/{id}：龙岛之前的版本，产出物、材料、产出数量完整且权威
  - Wowhead 职业法术列表（/spells/professions/<slug>）：龙岛及以后版本的
    配方法术 ID、产出物、产出数量（creates=[item,min,max]）、材料
    （Blizzard 对这些版本 crafted_item 为 null，材料也只给一部分）
  - Wowhead 制造物品列表（/items?filter=86:166）：同一配方的所有品质等级产出物
    及其 qualityTier（★ 等级）

旧版本的问题（已修复）：
  1. 按名字搜 Wowhead 后把所有搜索结果都当成该配方入库，
     例如附魔 "Speed" 被关联到 23 个各版本不相干的法术，版本/名字全部错乱
  2. output_qty 恒为 1，药水等一次产出 5 个的配方收入被低估 5 倍
  3. 只存了一个品质的产出物，且品质星级按物品 ID 猜测（实际顺序常相反）
  4. 用 Blizzard recipe_id 冒充 spell_id 去取 tooltip（两者不是同一个 ID）

用法：
  python build_db.py            → 全量重建（删除旧库）
  python build_db.py --refresh  → 忽略本地缓存，重新抓取所有数据
"""

import sys, io, os, re, json, time, sqlite3, statistics, requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

load_dotenv()
CLIENT_ID     = os.environ["BNET_CLIENT_ID"]
CLIENT_SECRET = os.environ["BNET_CLIENT_SECRET"]
API_BASE      = "https://us.api.blizzard.com"
TOKEN_URL     = "https://oauth.battle.net/token"
WH_HEADERS    = {"User-Agent": "Mozilla/5.0"}
DB_FILE       = "wow_recipes.db"
CACHE_DIR     = "build_cache"

# 制造业职业 ID -> Wowhead 法术列表 slug / 物品过滤器 "Crafted by profession" 取值
PROFESSIONS = {
    164: ("blacksmithing",  2),
    165: ("leatherworking", 8),
    171: ("alchemy",        1),
    185: ("cooking",        3),
    197: ("tailoring",     10),
    202: ("engineering",    5),
    333: ("enchanting",     4),
    755: ("jewelcrafting",  7),
    773: ("inscription",   15),
}
# Dragon Isles 及以后才有制作品质分级，Blizzard 也不再返回 crafted_item
QUALITY_TIER_MIN = 2822
# Wowhead 物品过滤器 166（资料片）：10=巨龙时代 11=地心之战 12=至暗之夜
WH_EXPANSIONS = (10, 11, 12)

REFRESH = False


# ──────────────────────────────────────────────
# 通用：缓存 + 请求
# ──────────────────────────────────────────────

def save_cache(name, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def cached(name, fn):
    path = os.path.join(CACHE_DIR, name)
    if not REFRESH and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    data = fn()
    save_cache(name, data)
    return data


def http_get(url, retries=5, **kw):
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=60, **kw)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                r.raise_for_status()
            print(f"    [重试] {r.status_code} {url}")
        except requests.HTTPError:
            raise
        except requests.RequestException as e:
            print(f"    [重试] {e}")
        time.sleep(2 * (2 ** attempt))
    raise Exception(f"请求失败，已重试 {retries} 次：{url}")


# ──────────────────────────────────────────────
# Blizzard API
# ──────────────────────────────────────────────

_token = None

def bz(path, locale="en_US"):
    global _token
    if _token is None:
        r = requests.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                          auth=(CLIENT_ID, CLIENT_SECRET), timeout=10)
        r.raise_for_status()
        _token = r.json()["access_token"]
    return http_get(f"{API_BASE}{path}",
                    params={"namespace": "static-us", "locale": locale},
                    headers={"Authorization": f"Bearer {_token}"}).json()


def fetch_bz_tiers():
    """返回 [{prof, prof_name, prof_zh, tier, tier_name, recipes:[{id,name,name_zh,category}]}]"""
    jobs = []
    for prof_id in PROFESSIONS:
        en = bz(f"/data/wow/profession/{prof_id}")
        zh = bz(f"/data/wow/profession/{prof_id}", "zh_CN")
        for t in en.get("skill_tiers", []):
            jobs.append((prof_id, en["name"], zh["name"], t["id"], t["name"]))

    def one(job):
        prof_id, pn, pz, tid, tn = job
        en = bz(f"/data/wow/profession/{prof_id}/skill-tier/{tid}")
        zh = bz(f"/data/wow/profession/{prof_id}/skill-tier/{tid}", "zh_CN")
        zh_map = {r["id"]: r["name"] for c in zh.get("categories", []) for r in c["recipes"]}
        recipes = [{"id": r["id"], "name": r["name"], "name_zh": zh_map.get(r["id"]),
                    "category": c["name"]}
                   for c in en.get("categories", []) for r in c["recipes"]]
        return {"prof": prof_id, "prof_name": pn, "prof_zh": pz,
                "tier": tid, "tier_name": tn, "recipes": recipes}

    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, jobs))


def fetch_bz_recipes(recipe_ids):
    """{recipe_id: {crafted_item, alliance_crafted_item, horde_crafted_item, reagents, crafted_quantity}}"""
    keys = ("crafted_item", "alliance_crafted_item", "horde_crafted_item",
            "reagents", "crafted_quantity")

    def one(rid):
        try:
            d = bz(f"/data/wow/recipe/{rid}")
            return str(rid), {k: d.get(k) for k in keys}
        except Exception as e:
            print(f"  [ERR] recipe {rid}: {e}")
            return str(rid), None

    with ThreadPoolExecutor(16) as ex:
        return dict(ex.map(one, recipe_ids))


# ──────────────────────────────────────────────
# Wowhead
# ──────────────────────────────────────────────

def _wh_listview(html, var):
    m = re.search(rf"var {var} = (\[.*?\]);\n", html, re.S)
    if not m:
        return []
    # Wowhead 的 JS 对象里混有未加引号的 key（quality:、popularity:、frommerge: 等）
    return json.loads(re.sub(r'([,{])([a-zA-Z_]+):', r'\1"\2":', m.group(1)))


def fetch_wh_spells(slug):
    """职业法术列表：[{id, name, skill, creates:[item,min,max], reagents:[[item,qty]]}]"""
    html = http_get(f"https://www.wowhead.com/spells/professions/{slug}", headers=WH_HEADERS).text
    time.sleep(1)
    return [{k: s.get(k) for k in ("id", "name", "skill", "creates", "reagents")}
            for s in _wh_listview(html, "listviewspells")]


def fetch_wh_items(filter_value, expansion):
    """某职业某资料片的制造物品：{item_id: {spells:[spell_id], quality_tier}}"""
    html = http_get(f"https://www.wowhead.com/items?filter=86:166;{filter_value}:{expansion};0:0",
                    headers=WH_HEADERS).text
    time.sleep(1)
    tiers = {}
    for g in re.finditer(r"WH\.Gatherer\.addData\(3, \d+, (\{.*?\})\);\n", html, re.S):
        try:
            for k, v in json.loads(g.group(1)).items():
                tiers[int(k)] = v.get("qualityTier", 0)
        except ValueError:
            pass
    out = {}
    for it in _wh_listview(html, "listviewitems"):
        spells = [sm["ti"] for sm in it.get("sourcemore", []) if sm.get("t") == 6 and "ti" in sm]
        if spells:
            out[str(it["id"])] = {"spells": spells, "quality_tier": tiers.get(it["id"], 0),
                                  "name": it.get("name")}
    if len(out) >= 1000:
        print(f"  [警告] Wowhead 物品列表可能被截断：filter={filter_value} 资料片={expansion}")
    return out


def wh_search_spells(name, prof_id):
    """
    兜底：Wowhead 搜索，只接受 sourcemore 中 t=6（制造）、s=职业、n=配方名 完全一致的法术。
    返回 [spell_id]（去重，保持顺序）。
    """
    r = http_get("https://www.wowhead.com/search", params={"q": name, "json": 1}, headers=WH_HEADERS)
    time.sleep(1.5)
    found = []
    for m in re.finditer(r'"sourcemore":(\[.*?\])', r.text):
        try:
            for sm in json.loads(m.group(1)):
                if (sm.get("t") == 6 and sm.get("s") == prof_id
                        and sm.get("n", "").lower() == name.lower() and sm.get("ti") not in found):
                    found.append(sm["ti"])
        except ValueError:
            pass
    return found


def wh_spell_page(spell_id):
    """从法术页面解析 g_spells 数据：{id, name, skill, creates, reagents}"""
    try:
        html = http_get(f"https://www.wowhead.com/spell={spell_id}", headers=WH_HEADERS).text
    except Exception as e:
        print(f"    [--] spell {spell_id}: {e}")
        return None
    time.sleep(1.5)
    m = re.search(rf"\$\.extend\(g_spells\[{spell_id}\], (\{{.*?\}})\);", html)
    if not m:
        return None
    s = json.loads(re.sub(r'([,{])([a-zA-Z_]+):', r'\1"\2":', m.group(1)))
    return {k: s.get(k) for k in ("id", "name", "skill", "creates", "reagents")}


# ──────────────────────────────────────────────
# 解析
# ──────────────────────────────────────────────

def tier_expansion(tier_id):
    """技能层 -> Wowhead 资料片编号（10=巨龙时代 11=地心之战 12=至暗之夜）"""
    return 10 if tier_id < 2860 else 11 if tier_id < 2900 else 12


def clean_name(name):
    # Midnight 部分配方名带品质图标标记，如 "Refine X |A:Professions-ChatIcon-Quality-12-Tier2:20:20|a"
    return re.sub(r"\s*\|A:.*?\|a", "", name).strip()


def resolve_old(rec, bz_rec, spells_by_item):
    """龙岛之前：直接用 Blizzard 数据。返回 dict 或 None（无产出物，如直接附在装备上的附魔）。"""
    if not bz_rec:
        return None
    item = bz_rec.get("crafted_item") or bz_rec.get("alliance_crafted_item") or bz_rec.get("horde_crafted_item")
    reagents = [(r["reagent"]["id"], r["quantity"]) for r in (bz_rec.get("reagents") or [])]
    if not item or not reagents:
        return None
    outputs = [item["id"]]
    for k in ("alliance_crafted_item", "horde_crafted_item"):
        if bz_rec.get(k) and bz_rec[k]["id"] not in outputs:
            outputs.append(bz_rec[k]["id"])
    cq = bz_rec.get("crafted_quantity") or {}
    qty = cq.get("value") or ((cq.get("minimum", 1) + cq.get("maximum", 1)) / 2 if cq else 1)
    # 同一物品可能有多个配方（如不同转化），优先同名法术
    cands = spells_by_item.get(item["id"], [])
    same = [sp for sp in cands if sp["name"].lower() == clean_name(rec["name"]).lower()]
    spell = (same or (cands if len(cands) == 1 else [None]))[0]
    return {"spell_id": spell and spell["id"], "crafted_item_id": item["id"],
            "output_qty": qty, "reagents": reagents,
            "outputs": [(i, None) for i in outputs], "source": "blizzard"}


def resolve_new(spell, items_by_spell, quality_of):
    """龙岛及以后：用 Wowhead 法术数据。"""
    creates, reagents = spell.get("creates"), spell.get("reagents")
    if not creates or not reagents:
        return None
    item_ids = list(dict.fromkeys([creates[0]] + items_by_spell.get(spell["id"], [])))
    outputs = [(i, quality_of.get(i) or None) for i in item_ids]
    return {"spell_id": spell["id"], "crafted_item_id": creates[0],
            "output_qty": max(1, (creates[1] + creates[2]) / 2),   # Wowhead 对部分装饰品给 0
            "reagents": [(i, q) for i, q in reagents if i not in item_ids],
            "outputs": outputs, "source": "wowhead"}


# ──────────────────────────────────────────────
# SQLite
# ──────────────────────────────────────────────

SCHEMA = """
    CREATE TABLE professions (
        id      INTEGER PRIMARY KEY,
        name    TEXT NOT NULL,
        name_zh TEXT
    );
    CREATE TABLE skill_tiers (
        id            INTEGER PRIMARY KEY,
        profession_id INTEGER NOT NULL REFERENCES professions(id),
        name          TEXT NOT NULL
    );
    -- recipe_id = Blizzard 配方 ID（与 WoW spell_id 不同）
    CREATE TABLE recipes (
        recipe_id       INTEGER PRIMARY KEY,
        spell_id        INTEGER,
        profession_id   INTEGER NOT NULL REFERENCES professions(id),
        skill_tier_id   INTEGER NOT NULL REFERENCES skill_tiers(id),
        category        TEXT,
        name            TEXT NOT NULL,
        name_zh         TEXT,
        crafted_item_id INTEGER,
        output_qty      REAL NOT NULL DEFAULT 1,   -- 每次制作的平均产出数量
        source          TEXT                        -- blizzard / wowhead
    );
    CREATE TABLE recipe_reagents (
        recipe_id INTEGER NOT NULL REFERENCES recipes(recipe_id),
        item_id   INTEGER NOT NULL,
        quantity  INTEGER NOT NULL
    );
    -- quality_rank：龙岛+ 制作品质（1..3，至暗之夜为 1..2）；旧版本为 NULL
    CREATE TABLE recipe_outputs (
        recipe_id    INTEGER NOT NULL REFERENCES recipes(recipe_id),
        item_id      INTEGER NOT NULL,
        quality_rank INTEGER
    );
    CREATE INDEX idx_reagents_recipe ON recipe_reagents(recipe_id);
    CREATE INDEX idx_outputs_recipe  ON recipe_outputs(recipe_id);
"""


def write_db(tiers, resolved):
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    conn = sqlite3.connect(DB_FILE)
    conn.executescript(SCHEMA)
    for t in tiers:
        conn.execute("INSERT OR IGNORE INTO professions VALUES (?,?,?)",
                     (t["prof"], t["prof_name"], t["prof_zh"]))
        conn.execute("INSERT INTO skill_tiers VALUES (?,?,?)", (t["tier"], t["prof"], t["tier_name"]))
    for t, rec, r in resolved:
        conn.execute("INSERT INTO recipes VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (rec["id"], r["spell_id"], t["prof"], t["tier"], rec["category"],
                      clean_name(rec["name"]), rec["name_zh"] and clean_name(rec["name_zh"]),
                      r["crafted_item_id"], r["output_qty"], r["source"]))
        conn.executemany("INSERT INTO recipe_reagents VALUES (?,?,?)",
                         [(rec["id"], i, q) for i, q in r["reagents"]])
        conn.executemany("INSERT INTO recipe_outputs VALUES (?,?,?)",
                         [(rec["id"], i, q) for i, q in r["outputs"]])
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

def build():
    print("[1/4] Blizzard 职业 / 技能层 / 配方列表")
    tiers = cached("bz_tiers.json", fetch_bz_tiers)
    all_ids = sorted({r["id"] for t in tiers for r in t["recipes"]})
    print(f"  {len(tiers)} 个技能层，{len(all_ids)} 个配方")

    print("[2/4] Blizzard 配方详情")
    bz_recipes = cached("bz_recipes.json", lambda: fetch_bz_recipes(all_ids))

    print("[3/4] Wowhead 法术 / 制造物品列表")
    wh_spells, items_by_spell, quality_of, item_exp = {}, defaultdict(list), {}, {}
    spells_by_item_name = defaultdict(list)
    for prof_id, (slug, fv) in PROFESSIONS.items():
        spells = cached(f"wh_spells_{prof_id}.json", lambda: fetch_wh_spells(slug))
        wh_spells[prof_id] = [s for s in spells if prof_id in (s.get("skill") or [])]
        for exp in WH_EXPANSIONS:
            items = cached(f"wh_items_{prof_id}_{exp}.json", lambda: fetch_wh_items(fv, exp))
            for iid, it in items.items():
                quality_of[int(iid)] = it["quality_tier"]
                item_exp[int(iid)] = exp
                for sid in it["spells"]:
                    items_by_spell[sid].append(int(iid))
                    if it.get("name"):
                        lst = spells_by_item_name[(prof_id, it["name"].lower())]
                        if sid not in lst:
                            lst.append(sid)
    extra_spells = cached("wh_extra_spells.json", lambda: {})

    print("[4/4] 匹配配方")
    resolved, skipped = {}, defaultdict(int)
    for t in tiers:
        prof_id = t["prof"]
        spells = wh_spells[prof_id]
        if t["tier"] < QUALITY_TIER_MIN:
            by_item, by_name_old = defaultdict(list), defaultdict(list)
            for s in spells:
                if s.get("creates"):
                    by_item[s["creates"][0]].append(s)
                    by_name_old[s["name"].lower()].append(s)
            for rec in t["recipes"]:
                r = resolve_old(rec, bz_recipes.get(str(rec["id"])), by_item)
                if not r:
                    # Blizzard 没给产出物（如熊猫人宝石），按名字唯一匹配 Wowhead
                    cands = by_name_old.get(clean_name(rec["name"]).lower(), [])
                    if len(cands) == 1:
                        r = resolve_new(cands[0], items_by_spell, quality_of)
                if r:
                    resolved[(id(t), rec["id"])] = (t, rec, r)
                else:
                    skipped[t["tier_name"]] += 1
            continue

        by_name = defaultdict(list)
        for s in spells:
            if s.get("creates"):
                by_name[s["name"].lower()].append(s)
        # 法术存在但不产出固定物品（附录说明、重铸、随机转化、分解等）
        no_item = {s["name"].lower() for s in spells if not s.get("creates")} - set(by_name)

        # 同名配方（如多个 rank 的 Writ）按 ID 顺序一一对应
        names = defaultdict(list)
        for rec in t["recipes"]:
            names[clean_name(rec["name"]).lower()].append(rec)

        # 多个同名候选时：优先产出物属于本资料片的，其次法术 ID 离本技能层中位数最近的
        exp = tier_expansion(t["tier"])
        unique_ids = [by_name[n][0]["id"] for n in names if len(by_name.get(n, [])) == 1]
        anchor = statistics.median(unique_ids) if unique_ids else 0

        def rank(s):
            e = item_exp.get(s["creates"][0])
            return (0 if e == exp else 1 if e is None else 2, abs(s["id"] - anchor))

        for n, recs in names.items():
            cands = [s for s in by_name.get(n, []) if item_exp.get(s["creates"][0], exp) == exp]
            if not cands and n not in no_item:
                # 列表里没有（Wowhead 烹饪列表不全），逐个搜索
                key = f"{prof_id}:{n}"
                if key not in extra_spells:
                    sids = spells_by_item_name.get((prof_id, n))
                    if not sids:
                        try:
                            sids = wh_search_spells(clean_name(recs[0]["name"]), prof_id)
                        except Exception as e:
                            print(f"    [--] 搜索失败 {n}: {e}")
                            continue
                    extra_spells[key] = [sp for sp in (wh_spell_page(s) for s in sids) if sp]
                    if len(extra_spells[key]) == len(sids):   # 有失败的不缓存，下次重试
                        save_cache("wh_extra_spells.json", extra_spells)
                cands = [s for s in extra_spells[key]
                         if s.get("creates") and item_exp.get(s["creates"][0], exp) == exp]
            if not cands:
                skipped[t["tier_name"]] += len(recs)
                continue
            cands = sorted(cands, key=rank)[:len(recs)]
            cands.sort(key=lambda s: s["id"])
            for rec, spell in zip(sorted(recs, key=lambda r: r["id"]), cands):
                r = resolve_new(spell, items_by_spell, quality_of)
                if r:
                    resolved[(id(t), rec["id"])] = (t, rec, r)
                else:
                    skipped[t["tier_name"]] += 1
            skipped[t["tier_name"]] += max(0, len(recs) - len(cands))

    # 同一配方 ID 可能出现在多个技能层（极少），保留第一次
    final, seen = [], set()
    for t, rec, r in resolved.values():
        if rec["id"] not in seen:
            seen.add(rec["id"])
            final.append((t, rec, r))
    write_db(tiers, final)

    print(f"\n入库：{len(final)} 个配方；未入库（无产出物/非制造类/找不到数据）：{sum(skipped.values())} 个")
    for name, n in sorted(skipped.items(), key=lambda x: -x[1])[:15]:
        print(f"  {name}: 跳过 {n}")


def main():
    global REFRESH
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    REFRESH = "--refresh" in sys.argv[1:]
    build()
    print(f"[OK] 完成，数据库已保存：{DB_FILE}")


if __name__ == "__main__":
    main()
