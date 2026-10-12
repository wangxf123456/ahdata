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
SNAPSHOT  = "commodities_raw.json"
BATCH         = 20   # 按连续制作 20 次估算材料买入均价
SELL_MIN_QTY  = 5    # 产出物全区挂单少于此数视为无市场


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


def buy_price(price_map, item_id, units):
    """
    买入 units 个的平均单价（从最低价往上吃单）。挂单总量不足返回 None。
    比只看最低价更真实：低价挂单往往只有几个，批量制作时要吃到更贵的单。
    """
    need, spent = units, 0
    for p, q in price_map.get(item_id, []):
        take = min(q, need)
        spent += p * take
        need  -= take
        if need <= 0:
            return spent / units
    return None


def sell_price(price_map, item_id, min_total_qty=SELL_MIN_QTY):
    """
    卖价 = 当前最低挂单价（你要挂得比它低才能卖出）。
    市场上总量太少的物品价格不可信，返回 None。
    """
    listings = price_map.get(item_id, [])
    if sum(q for _, q in listings) < min_total_qty:
        return None
    return listings[0][0]


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
    where, args = "", []
    if prof_ids:
        where = "WHERE r.profession_id IN ({})".format(",".join("?" * len(prof_ids)))
        args = list(prof_ids)
    rows = conn.execute(f"""
        SELECT r.recipe_id, r.name_zh, r.name, r.output_qty, r.category,
               r.profession_id, p.name_zh, p.name, r.skill_tier_id, t.name
        FROM recipes r
        JOIN professions p ON p.id = r.profession_id
        JOIN skill_tiers t ON t.id = r.skill_tier_id
        {where}
    """, args).fetchall()

    reagents, outputs = defaultdict(list), defaultdict(list)
    for rid, iid, qty in conn.execute("SELECT recipe_id, item_id, quantity FROM recipe_reagents"):
        reagents[rid].append((iid, qty))
    for rid, iid, rank in conn.execute("SELECT recipe_id, item_id, quality_rank FROM recipe_outputs"):
        outputs[rid].append((iid, rank))

    return [{
        "recipe_id":  rid,
        "name":       name_zh or name,
        "name_en":    name,
        "output_qty": output_qty,
        "category":   category,
        "prof_id":    prof_id,
        "prof_name":  prof_zh or prof_en,
        "tier_id":    tier_id,
        "tier_name":  tier_name,
        "reagents":   reagents[rid],
        "outputs":    sorted(outputs[rid], key=lambda o: o[1] or 0),
    } for rid, name_zh, name, output_qty, category, prof_id, prof_zh, prof_en, tier_id, tier_name in rows]


def analyze(recipes, price_map):
    """
    每个配方计算：
      cost        — 材料按吃单均价买 BATCH 次制作所需数量（龙岛+ 材料用最低品质）
      profit_low  — 卖出最低品质产出的利润（保守，任何技能都能做出）
      profit      — 卖出最高品质产出的利润（需要足够技能/专注/高品质材料）
    """
    results = []
    for rec in recipes:
        if not rec["outputs"]:
            continue
        cost, mats = 0, []
        for item_id, qty in rec["reagents"]:
            p = buy_price(price_map, item_id, qty * BATCH)
            if p is None:
                cost = None
                break
            cost += p * qty
            mats.append((item_id, qty, int(p)))
        if cost is None:
            continue

        priced = []
        for oid, rank in rec["outputs"]:
            p = sell_price(price_map, oid)
            if p is not None:
                _, qty, listings = market_info(price_map, oid, min_copper=0)
                priced.append({"id": oid, "rank": rank, "price": p, "qty": qty, "listings": listings})
        if not priced:
            continue

        def revenue(o):
            return int(o["price"] * rec["output_qty"] * (1 - AH_FEE))

        low  = priced[0]
        best = max(priced, key=lambda o: o["price"])
        rank_str = f" R{best['rank']}" if best["rank"] else ""
        results.append({
            **{k: rec[k] for k in ("recipe_id", "name_en", "category", "prof_id",
                                   "prof_name", "tier_id", "tier_name", "output_qty")},
            "name":          rec["name"] + rank_str,
            "crafted_id":    best["id"],
            "rank":          best["rank"],
            "cost":          int(cost),
            "sell_price":    best["price"],
            "sell_total":    revenue(best),
            "profit":        revenue(best) - int(cost),
            "low_rank":      low["rank"],
            "profit_low":    revenue(low) - int(cost),
            "materials":     mats,
            "sell_qty":      best["qty"],
            "sell_listings": best["listings"],
            "ranks":         [(o["rank"], o["id"], o["price"], o["qty"]) for o in priced],
        })

    results.sort(key=lambda x: x["profit"], reverse=True)
    return results


_PROF_SUFFIXES = [
    "Blacksmithing", "Leatherworking", "Alchemy", "Cooking", "Tailoring",
    "Engineering", "Enchanting", "Jewelcrafting", "Inscription",
]

def _expansion_name(tier_name):
    """从 tier 名称提取版本名，去掉职业后缀。"""
    name = tier_name
    for s in _PROF_SUFFIXES:
        name = name.replace(s, "")
    return name.strip(" /").strip() or tier_name


def _group_by_tier(results):
    """
    按版本（expansion）分组，版本内再按职业分组。
    版本按 tier_id 降序（新版本在前），职业内按利润降序。
    返回 [(expansion_name, tier_id, [(prof_name, [recipe, ...])])]
    """
    tier_map = defaultdict(lambda: defaultdict(list))
    tier_ids = {}
    for r in results:
        exp = _expansion_name(r["tier_name"])
        tier_map[exp][r["prof_name"]].append(r)
        tier_ids[exp] = max(tier_ids.get(exp, 0), r["tier_id"])

    grouped = []
    for exp, profs in tier_map.items():
        for v in profs.values():
            v.sort(key=lambda x: x["profit"], reverse=True)
        prof_list = sorted(profs.items(), key=lambda kv: kv[1][0]["profit"], reverse=True)
        grouped.append((exp, tier_ids[exp], prof_list))

    grouped.sort(key=lambda x: x[1], reverse=True)
    return grouped


def _group_by_prof(results):
    """按职业分组，组间按组内最高利润降序，组内按利润降序。"""
    groups = defaultdict(list)
    for r in results:
        groups[r["prof_name"]].append(r)
    for v in groups.values():
        v.sort(key=lambda x: x["profit"], reverse=True)
    return sorted(groups.items(), key=lambda kv: kv[1][0]["profit"], reverse=True)


def print_results(results):
    profitable = [r for r in results if r["profit"] > 0]
    losing     = [r for r in results if r["profit"] <= 0]

    print(f"\n{'='*110}")
    print(f"  全职业套利分析  —  盈利 {len(profitable)} 个 / 亏损 {len(losing)} 个")
    print(f"{'='*110}")
    print(f"  {'配方':<26}  {'产出':>4}  {'材料成本':>12}  {'卖合计':>12}  {'最高品质利润':>12}  {'最低品质利润':>12}  {'挂单量':>6}")
    print(f"  {'-'*108}")

    for exp, tier_id, profs in _group_by_tier(profitable):
        print(f"\n  ══ {exp} ══")
        for prof_name, items in profs:
            print(f"\n    ── {prof_name} ──")
            for r in items:
                print(f"  {r['name']:<26}  {r['output_qty']:>4g}  "
                      f"{g(r['cost']):>12}  {g(r['sell_total']):>12}  "
                      f"{g(r['profit']):>12}  {g(r['profit_low']):>12}  {r['sell_qty']:>6}")

    if losing:
        print(f"\n\n  亏损配方共 {len(losing)} 个（略）")


# ── 珠宝加工选矿（12.0.7）──────────────────────────────────────
# 每次选矿消耗 5 个矿石；各宝石概率独立判定（非互斥）
# 概率数据来源：wow-professions.com Midnight JC Guide
PROSPECT_GEMS = {
    242553: "血色榴石",
    242554: "阿曼尼青金石",
    242606: "晦暗紫晶",
    242607: "哈籁恩达尔榄石",
    242610: "无瑕哈籁恩达尔榄石",
    242611: "无瑕晦暗紫晶",
    242612: "无瑕阿曼尼青金石",
    242613: "无瑕血色榴石",
}

PROSPECT_ORES = [
    # (ore_id, name_zh, [(gem_id, prob), ...])
    (237359, "折射铜矿石", [
        (242553, 0.08), (242554, 0.08), (242606, 0.08), (242607, 0.08),
    ]),
    (237362, "暗银锡矿石", [
        (242607, 0.12), (242606, 0.12), (242610, 0.12), (242611, 0.12),
    ]),
    (237364, "辉熠银矿石", [
        (242553, 0.12), (242554, 0.12), (242613, 0.12), (242612, 0.12),
    ]),
    (237366, "炫目瑟银", [
        (242613, 0.15), (242612, 0.15), (242610, 0.15), (242611, 0.15),
    ]),
]


def _prospect_section(price_map):
    lines = [
        "## 珠宝加工选矿效率分析（12.0.7）",
        "",
        "> 每次选矿消耗 5 个矿石。各宝石独立判定，概率来自 wow-professions.com。",
        "> 炫目瑟银另有 22% 概率产出永歌钻石（需解锁专精），此处未计入。",
        "",
    ]

    for ore_id, ore_name, yields in PROSPECT_ORES:
        ore_price, ore_qty, _ = market_info(price_map, ore_id)
        if not ore_price:
            continue

        cost = ore_price * 5
        total_ev = 0
        gem_rows = []
        for gem_id, prob in yields:
            gp, gqty, _ = market_info(price_map, gem_id)
            if not gp:
                continue
            ev = int(gp * prob)
            total_ev += ev
            gem_rows.append((PROSPECT_GEMS.get(gem_id, str(gem_id)), gp, prob, ev, gqty))

        if not gem_rows:
            continue

        profit    = total_ev - cost
        sign      = "✓ 盈利" if profit > 0 else "✗ 亏损"

        lines.append(f"### {ore_name}（{g(ore_price)}/个 × 5 = {g(cost)}，挂单={ore_qty}）")
        lines.append("")
        lines.append("| 宝石 | 市价 | 概率 | 期望产值 | 宝石挂单量 |")
        lines.append("|---|---|---|---|---|")
        for gname, gp, prob, ev, gqty in gem_rows:
            lines.append(f"| {gname} | {g(gp)} | {prob:.0%} | {g(ev)} | {gqty} |")
        lines.append("")
        lines.append(f"**5矿期望宝石产值：{g(total_ev)}　成本：{g(cost)}　差额：{g(profit)}　{sign}**")
        lines.append("")

    return lines


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
             f"> 材料成本按连续制作 {BATCH} 次的吃单均价计算；卖价为最低挂单价，已扣 {AH_FEE:.0%} 手续费。",
             f"> “最高品质利润”假设能做出最高品质（R2/R3）；“最低品质利润”为保守值。未计入多重制作/省料加成。\n",
             f"## 盈利配方\n"]

    for exp, tier_id, profs in _group_by_tier(profitable):
        lines.append(f"## {exp}\n")
        for prof_name, items in profs:
            lines.append(f"### {prof_name}\n")
            lines.append("| 配方 | 每次产出 | 材料成本 | 卖合计 | 最高品质利润 | 利润率 | 最低品质利润 | 挂单量 | 卖家数 |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for r in items:
                margin = int(r["profit"] / r["cost"] * 100) if r["cost"] else 0
                lines.append(f"| {r['name']} | {r['output_qty']:g} | {g(r['cost'])} | {g(r['sell_total'])} | "
                             f"{g(r['profit'])} | {margin}% | {g(r['profit_low'])} | "
                             f"{r['sell_qty']} | {r['sell_listings']} |")
            lines.append("")

    lines.append(f"---\n亏损配方共 {len(losing)} 个（略）\n")

    if price_map:
        lines.append("")
        lines.extend(_prospect_section(price_map))
        lines.append("")
        lines.extend(_recycle_section(price_map))

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[OK] 已保存 {path}")


# ──────────────────────────────────────────────
# 主程序
# ──────────────────────────────────────────────

def main():
    # python ah_analyzer.py [prof_id ...] [--cached]
    #   --cached  复用上次保存的 commodities_raw.json，不重新拉取
    args = sys.argv[1:]
    prof_ids = [int(x) for x in args if x.isdigit()] or None

    if "--cached" in args and os.path.exists(SNAPSHOT):
        with open(SNAPSHOT, encoding="utf-8") as f:
            auctions = json.load(f)
        print(f"[OK] 读取本地快照 {SNAPSHOT}：{len(auctions)} 条挂单")
    else:
        auctions = fetch_commodities(get_token())
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(auctions, f)
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
