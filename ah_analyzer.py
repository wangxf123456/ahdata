"""
WoW 拍卖行套利分析
从 wow_recipes.db 读取配方，拉取实时 AH 快照，计算制作利润。
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import sqlite3, requests, json, os
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()
CLIENT_ID     = os.environ["BNET_CLIENT_ID"]
CLIENT_SECRET = os.environ["BNET_CLIENT_SECRET"]

REGION    = "us"
API_BASE  = f"https://{REGION}.api.blizzard.com"
TOKEN_URL = "https://oauth.battle.net/token"
AH_FEE    = 0.05
DB_FILE   = "wow_recipes.db"


# ──────────────────────────────────────────────
# Blizzard API
# ──────────────────────────────────────────────

def get_token():
    r = requests.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                      auth=(CLIENT_ID, CLIENT_SECRET), timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_commodities(token):
    print("[...] 拉取全区商品快照...")
    r = requests.get(f"{API_BASE}/data/wow/auctions/commodities",
                     params={"namespace": f"dynamic-{REGION}"},
                     headers={"Authorization": f"Bearer {token}", "Cache-Control": "no-cache"}, timeout=60)
    r.raise_for_status()
    auctions = r.json().get("auctions", [])
    print(f"[OK] {len(auctions)} 条挂单")
    return auctions



# ──────────────────────────────────────────────
# 价格计算
# ──────────────────────────────────────────────

def build_price_map(auctions):
    # { item_id: sorted list of (unit_price, quantity) }
    m = defaultdict(list)
    for a in auctions:
        item_id  = a.get("item", {}).get("id")
        price    = a.get("unit_price", 0)
        quantity = a.get("quantity", 1)
        if item_id and price > 0:
            m[item_id].append((price, quantity))
    return {k: sorted(v) for k, v in m.items()}


def market_info(price_map, item_id, min_copper=500):
    """返回 (floor_price, total_qty, num_listings)，过滤5银以下挂单。"""
    listings = [(p, q) for p, q in price_map.get(item_id, []) if p >= min_copper]
    if not listings:
        return None, 0, 0
    total_qty   = sum(q for _, q in listings)
    num_listings = len(listings)
    return listings[0][0], total_qty, num_listings


def floor_price(price_map, item_id, min_copper=500, min_total_qty=5):
    price, total_qty, _ = market_info(price_map, item_id, min_copper)
    if price is None or total_qty < min_total_qty:
        return None
    return price


def g(copper):
    if copper is None:
        return "N/A"
    sign = "-" if copper < 0 else ""
    c = abs(int(copper))
    return f"{sign}{c // 10000}g{(c % 10000) // 100:02d}s"


# ──────────────────────────────────────────────
# 套利分析
# ──────────────────────────────────────────────

def load_recipes(conn, prof_ids=None):
    # 过滤掉不在商品拍卖行的品类（装备、坐骑、家具等走普通拍卖行，价格不准）
    EXCLUDE_CATEGORIES = (
        # 通用占位
        "Appendix II - Stats",
        # 装备（铁匠）
        "Weapons", "Armor", "Competitor's Plate (PvP)",
        # 装备（制皮）
        "Mail Armor", "Leather Armor",
        "Competitor's Mail Armor", "Competitor's Leather Armor", "Mounts",
        # 装备（裁缝）
        "Garments", "Arcanoweave Garments", "Sunfire Silk Garments",
        "Competitor's Crafts (PvP)", "Wardrobe Enhancements",
        # 装备（工程）
        "Plate Equipment", "Mail Equipment", "Leather Equipment", "Cloth Equipment",
        "Guns", "Bots",
        # 装备（珠宝）
        "Crafting Couture", "Regal Rings", "Luxurious Lockets",
        "Competitor's Crafts (PvP)",
        # 装备（铭文）
        "Trinkets",
        # 附魔杖/魔杖（装备）
        "Rods", "Wands",
        # 家具（所有职业）
        "House Decor", "Stonework",
    )
    exclude_clause = " AND r.category NOT IN ({})".format(
        ",".join("?" * len(EXCLUDE_CATEGORIES))
    )

    if prof_ids:
        placeholders = ",".join("?" * len(prof_ids))
        rows = conn.execute(f"""
            SELECT r.spell_id, r.name_zh, r.name, r.crafted_item_id, r.output_qty,
                   r.category, r.profession_id, p.name_zh, p.name
            FROM recipes r
            JOIN professions p ON p.id = r.profession_id
            WHERE r.profession_id IN ({placeholders})
            {exclude_clause}
            ORDER BY r.profession_id, r.category, r.name
        """, prof_ids + list(EXCLUDE_CATEGORIES)).fetchall()
    else:
        rows = conn.execute(f"""
            SELECT r.spell_id, r.name_zh, r.name, r.crafted_item_id, r.output_qty,
                   r.category, r.profession_id, p.name_zh, p.name
            FROM recipes r
            JOIN professions p ON p.id = r.profession_id
            WHERE 1=1 {exclude_clause}
            ORDER BY r.profession_id, r.category, r.name
        """, list(EXCLUDE_CATEGORIES)).fetchall()

    result = []
    for spell_id, name_zh, name, crafted_id, output_qty, category, prof_id, prof_zh, prof_en in rows:
        reagents = conn.execute(
            "SELECT item_id, quantity FROM recipe_reagents WHERE spell_id = ?",
            (spell_id,)
        ).fetchall()
        output_ids_rows = conn.execute(
            "SELECT item_id FROM recipe_outputs WHERE spell_id = ?",
            (spell_id,)
        ).fetchall()
        output_ids = [r[0] for r in output_ids_rows] if output_ids_rows else ([crafted_id] if crafted_id else [])
        result.append({
            "spell_id":   spell_id,
            "name":       name_zh or name,
            "name_en":    name,
            "crafted_id": crafted_id,
            "output_ids": output_ids,
            "output_qty": output_qty,
            "category":   category,
            "prof_id":    prof_id,
            "prof_name":  prof_zh or prof_en,
            "reagents":   reagents,
        })
    return result


def analyze(recipes, price_map):
    results = []

    for rec in recipes:
        crafted_id = rec["crafted_id"]
        output_qty = rec["output_qty"]

        cost = 0
        mat_detail = []
        ok = True
        for item_id, qty in rec["reagents"]:
            p = floor_price(price_map, item_id)
            if p is None:
                ok = False
                break
            cost += p * qty
            mat_detail.append((item_id, qty, p))

        if not ok:
            continue

        # 查所有已知产出 ID，取流通性最好（挂单量最多）的那个
        best = None
        for oid in rec["output_ids"]:
            p = floor_price(price_map, oid)
            if p is None:
                continue
            _, qty, listings = market_info(price_map, oid)
            if best is None or qty > best["sell_qty"]:
                best = {"sell_id": oid, "sell_price": p, "sell_qty": qty, "sell_listings": listings}

        if best is None:
            continue

        sell_price   = best["sell_price"]
        sell_total   = int(sell_price * output_qty * (1 - AH_FEE))
        profit       = sell_total - cost
        results.append({
            "name":          rec["name"],
            "name_en":       rec["name_en"],
            "category":      rec["category"],
            "prof_id":       rec["prof_id"],
            "prof_name":     rec["prof_name"],
            "spell_id":      rec["spell_id"],
            "crafted_id":    best["sell_id"],
            "output_qty":    output_qty,
            "cost":          cost,
            "sell_price":    sell_price,
            "sell_total":    sell_total,
            "profit":        profit,
            "materials":     mat_detail,
            "sell_qty":      best["sell_qty"],
            "sell_listings": best["sell_listings"],
        })

    results.sort(key=lambda x: x["profit"], reverse=True)

    # 同一职业下同名配方去重，只保留利润最高的（避免多版本重复）
    seen = set()
    deduped = []
    for r in results:
        key = (r["prof_id"], r["name"], r["crafted_id"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped


def _group_by_prof(results):
    """按职业分组，组间按组内最高利润降序，组内按利润降序。"""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in results:
        groups[r["prof_name"]].append(r)
    for v in groups.values():
        v.sort(key=lambda x: x["profit"], reverse=True)
    return sorted(groups.items(), key=lambda kv: kv[1][0]["profit"], reverse=True)


def print_results(results):
    profitable = [r for r in results if r["profit"] > 0]
    losing     = [r for r in results if r["profit"] <= 0]

    print(f"\n{'='*100}")
    print(f"  全职业套利分析  —  盈利 {len(profitable)} 个 / 亏损 {len(losing)} 个")
    print(f"{'='*100}")
    print(f"  {'配方':<26}  {'材料成本':>12}  {'卖合计':>12}  {'总利润':>12}  {'挂单量':>6}  {'卖家数':>6}")
    print(f"  {'-'*98}")

    for prof_name, items in _group_by_prof(profitable):
        print(f"\n  ── {prof_name} ──")
        for r in items:
            print(f"  {r['name']:<26}  "
                  f"{g(r['cost']):>12}  {g(r['sell_total']):>12}  "
                  f"{g(r['profit']):>12}  "
                  f"{r['sell_qty']:>6}  {r['sell_listings']:>6}")

    if losing:
        print(f"\n\n  亏损配方共 {len(losing)} 个（略）")


# 循环回收输入材料（至暗之夜工程学可回收材料）
# 产出：以太流明(243578) + 永恒之核(243581)
RECYCLE_MATERIALS = [
    # (item_id, name_zh, MatQualityWeight)
    (237359, "折射铜矿石",    3),
    (237362, "暗银锡矿石",    4),
    (244697, "通量齿轮",      7),
    (244699, "滑油齿轮",      7),
    (244701, "完美齿轮",      7),
    (244703, "吻合齿轮",      7),
    (239702, "注魔亮麻布卷", 25),
    (243574, "轻歌齿轮",     28),
    (243576, "灵魂链齿",     31),
    (238518, "虚空淬炼兽皮", 80),
    (238520, "虚空淬炼铠甲", 80),
]
AETHERLUME_ID     = 243578
AETHERLUME_WEIGHT = 41


def _recycle_section(price_map):
    aether_price, aether_qty, _ = market_info(price_map, AETHERLUME_ID)
    if not aether_price:
        return []

    rows = []
    for iid, name, weight in RECYCLE_MATERIALS:
        price, qty, _ = market_info(price_map, iid)
        if not price:
            continue
        output_per_5 = (5 * weight) / AETHERLUME_WEIGHT
        cost_per = (price * 5 / output_per_5) if output_per_5 > 0 else float("inf")
        cheaper = cost_per < aether_price
        rows.append((cost_per, name, price, weight, output_per_5, cost_per, cheaper, qty))

    rows.sort(key=lambda x: x[0])

    lines = [
        "## 工程学循环回收 — 获取以太流明效率",
        f"",
        f"以太流明当前价格：**{g(aether_price)}**（item 243578）",
        f"",
        f"> 公式：5件材料总权重 ÷ {AETHERLUME_WEIGHT} = 产出以太流明数（未经游戏实测，仅供参考）",
        f"",
        "| 材料 | 材料价格 | 权重 | 5件产出以太流明 | 成本/以太流明 | 划算? | 挂单量 |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, name, price, weight, out, cost_per, cheaper, qty in rows:
        flag = "✓" if cheaper else "✗"
        lines.append(f"| {name} | {g(price)} | {weight} | {out:.2f} | {g(cost_per)} | {flag} | {qty} |")

    cheaper_rows = [r for r in rows if r[6]]
    if cheaper_rows:
        lines.append("")
        lines.append("**最优回收顺序（比直接买以太流明划算）：**")
        for i, (_, name, price, weight, out, cost_per, _, qty) in enumerate(cheaper_rows, 1):
            lines.append(f"{i}. {name}（{g(price)}/件，每5件出 {out:.1f} 个以太流明，折合 {g(cost_per)}/个）")

    return lines


def write_report(results, price_map=None, path="full_report.md"):
    profitable = [r for r in results if r["profit"] > 0]
    losing     = [r for r in results if r["profit"] <= 0]

    lines = [f"# WoW 全职业套利分析报告\n",
             f"盈利配方 {len(profitable)} 个 | 亏损配方 {len(losing)} 个\n",
             f"## 盈利配方\n"]

    for prof_name, items in _group_by_prof(profitable):
        lines.append(f"### {prof_name}\n")
        lines.append("| 配方 | 材料成本 | 卖合计 | 总利润 | 利润率 | 挂单量 | 卖家数 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in items:
            margin = int(r["profit"] / r["cost"] * 100) if r["cost"] else 0
            lines.append(f"| {r['name']} | {g(r['cost'])} | {g(r['sell_total'])} | {g(r['profit'])} | {margin}% | {r['sell_qty']} | {r['sell_listings']} |")
        lines.append("")

    lines.append(f"---\n亏损配方共 {len(losing)} 个（略）\n")

    if price_map:
        lines.append("")
        lines.extend(_recycle_section(price_map))

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[OK] 已保存 {path}")


# ──────────────────────────────────────────────
# 主程序
# ──────────────────────────────────────────────

def main():
    # 可选：python ah_analyzer.py 171 333  → 只跑指定职业
    prof_ids = [int(x) for x in sys.argv[1:]] if len(sys.argv) > 1 else None

    token = get_token()
    auctions = fetch_commodities(token)
    price_map = build_price_map(auctions)

    conn = sqlite3.connect(DB_FILE)
    recipes = load_recipes(conn, prof_ids)
    conn.close()
    print(f"[OK] 读取配方 {len(recipes)} 个")

    results = analyze(recipes, price_map)
    print_results(results)

    with open("analysis.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"[OK] 已保存 analysis.json")
    write_report(results, price_map)


if __name__ == "__main__":
    main()
