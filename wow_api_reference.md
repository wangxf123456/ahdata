# WoW Battle.net API 参考

实测验证（2026-06）。所有请求需在 Header 中携带 `Authorization: Bearer {token}`，不能放 query param。

---

## 认证

### Client Credentials（Game Data 用）
```
POST https://oauth.battle.net/token
Body: grant_type=client_credentials
Auth: HTTP Basic (CLIENT_ID, CLIENT_SECRET)
```
返回 `access_token`，有效期 24h。适用于所有 Game Data API 和 Search API。

### 用户 OAuth（Profile API 用）
需要用户登录授权，走标准 OAuth2 authorization_code 流程。
Client Credentials token 调用 Profile API 只会返回 403。

---

## 通用规则

| 参数 | 说明 |
|---|---|
| `namespace` | 必填，见下表 |
| `locale` | 可选，如 `en_US` / `zh_CN`，控制返回语言 |

### Namespace 对照

| Namespace | 用途 | 示例 |
|---|---|---|
| `static-us` | 静态游戏数据（物品、配方、职业等） | 版本更新时才变 |
| `dynamic-us` | 动态数据（拍卖行、服务器状态、Token 价格） | 实时变化 |
| `profile-us` | 角色/账号数据 | 需要用户 OAuth |

---

## Game Data API（static-us）

Base: `https://us.api.blizzard.com/data/wow`

### 拍卖行

| 端点 | Namespace | 说明 |
|---|---|---|
| `GET /auctions/commodities` | dynamic-us | **全区通用商品**挂单（药水/材料/宝石等），按物品池化，所有服务器共享价格 |
| `GET /auctions/connected-realm/{realmId}` | dynamic-us | 指定**互联服**的装备类拍卖行挂单（装备不跨服）|

`/auctions/commodities` 返回字段：
```json
{
  "auctions": [
    {"item": {"id": 211878}, "quantity": 50, "unit_price": 12500000}
  ]
}
```
> 注意：`unit_price` 单位为铜币（1金=10000铜）。买家总是优先买最低价，所以**卖价应取最低单价（floor price）**，不是中位价。

---

### 物品

| 端点 | 说明 |
|---|---|
| `GET /item/{itemId}` | 物品基础信息：名称、品质、等级、类型、售价、是否可装备等 |
| `GET /item-class/index` | 物品分类列表（武器/盔甲/消耗品等） |
| `GET /item-set/index` | 套装列表 |
| `GET /search/item` | 搜索物品，支持按名称/品质/等级过滤 |

`/item/{id}` 主要字段：`id`, `name`, `quality`, `level`, `required_level`, `item_class`, `item_subclass`, `inventory_type`, `purchase_price`, `sell_price`, `is_equippable`, `is_stackable`

`/search/item` 查询参数示例：
```
?namespace=static-us&name.en_US=flask&quality.type=EPIC&_pageSize=50&_page=1
```

---

### 职业与配方

| 端点 | 说明 |
|---|---|
| `GET /profession/index` | 所有职业列表（炼金/铁匠/附魔等） |
| `GET /profession/{profId}` | 职业详情，包含**所有技能层列表**（按版本分层） |
| `GET /profession/{profId}/skill-tier/{tierId}` | 指定技能层的**所有配方**，按类别分组 |
| `GET /recipe/{recipeId}` | 单个配方详情 |
| `GET /modified-crafting/index` | 改良制作系统索引（可选材料槽位类型等） |
| `GET /modified-crafting/category/index` | 改良制作分类 |
| `GET /modified-crafting/reagent-slot-type/index` | 可选材料槽位类型列表 |

**重要已知缺陷**：`/recipe/{id}` 对 TWW（至暗之夜）及更新版本的配方，`crafted_item` 字段全部返回 null。需用 Wowhead 补全产出物和材料数量信息。

`/profession/{profId}` 返回的 `skill_tiers` 示例：
```json
{"skill_tiers": [
  {"id": 2871, "name": "Khaz Algar Alchemy"},
  {"id": 2906, "name": "Midnight Alchemy"}   // 12.0.x 最新
]}
```
技能层 ID 越大 = 版本越新，取最大 ID 即当前版本内容。

---

### 服务器与地区

| 端点 | Namespace | 说明 |
|---|---|---|
| `GET /realm/index` | dynamic-us | 所有服务器列表 |
| `GET /realm/{slug}` | dynamic-us | 单个服务器信息（时区、类型、所属互联服） |
| `GET /connected-realm/index` | dynamic-us | 所有互联服列表 |
| `GET /connected-realm/{id}` | dynamic-us | 互联服详情（包含人口、排队状态、关联的拍卖行链接） |
| `GET /region/index` | dynamic-us | 地区列表（US/EU/KR/TW） |

`/realm/{slug}` 主要字段：`id`, `name`, `region`, `connected_realm`, `locale`, `timezone`, `type`（PvE/PvP）

---

### WoW Token

| 端点 | Namespace | 说明 |
|---|---|---|
| `GET /token/index` | dynamic-us | 当前 Token 金币价格（实时） |

返回：`{"price": 3200000000, "last_updated_timestamp": 1750000000}`（price 单位铜币）

---

### 大秘境（Mythic Keystone）

| 端点 | Namespace | 说明 |
|---|---|---|
| `GET /mythic-keystone/index` | dynamic-us | 当前赛季/副本总览 |
| `GET /mythic-keystone/dungeon/index` | dynamic-us | 所有大秘境副本列表 |
| `GET /mythic-keystone/dungeon/{dungeonId}` | dynamic-us | 副本详情 |
| `GET /mythic-keystone/period/index` | dynamic-us | 周期列表（每周重置） |
| `GET /mythic-keystone/period/{periodId}` | dynamic-us | 指定周期信息 |
| `GET /mythic-keystone/season/index` | dynamic-us | 所有赛季列表 |
| `GET /mythic-keystone/season/{seasonId}` | dynamic-us | 赛季详情 |
| `GET /connected-realm/{id}/mythic-leaderboard/index` | dynamic-us | 指定服务器组大秘境排行榜列表 |
| `GET /connected-realm/{id}/mythic-leaderboard/{dungeonId}/period/{periodId}` | dynamic-us | 指定副本和周期的排行榜（含每条记录的时间、阵容、层数） |

---

### 团队副本排行榜

| 端点 | Namespace | 说明 |
|---|---|---|
| `GET /leaderboard/hall-of-fame/{raidSlug}/{faction}` | dynamic-us | 荣誉殿堂（首杀排行），faction=alliance/horde |

---

### PvP

| 端点 | Namespace | 说明 |
|---|---|---|
| `GET /pvp-season/index` | dynamic-us | 所有 PvP 赛季列表 |
| `GET /pvp-season/{seasonId}` | dynamic-us | 赛季详情 |
| `GET /pvp-season/{seasonId}/pvp-leaderboard/index` | dynamic-us | 该赛季排行榜（各段位） |
| `GET /pvp-season/{seasonId}/pvp-leaderboard/{bracket}` | dynamic-us | 指定段位（2v2/3v3/rbg）排行榜 |
| `GET /pvp-season/{seasonId}/pvp-reward/index` | dynamic-us | 赛季奖励列表 |
| `GET /pvp-tier/index` | static-us | PvP 段位定义（非凡者/传奇等） |
| `GET /pvp-tier/{tierId}` | static-us | 段位详情 |

---

### 角色类型

| 端点 | 说明 |
|---|---|
| `GET /playable-class/index` | 所有可玩职业列表 |
| `GET /playable-class/{classId}` | 职业详情（可用专精、势力限制等） |
| `GET /playable-class/{classId}/pvp-talent-slots` | 该职业的 PvP 天赋槽 |
| `GET /playable-race/index` | 所有可玩种族列表 |
| `GET /playable-specialization/index` | 所有专精列表 |
| `GET /playable-specialization/{specId}` | 专精详情（职业、角色、能力） |

---

### 天赋

| 端点 | 说明 |
|---|---|
| `GET /talent-tree/index` | 天赋树列表（按职业/专精） |
| `GET /talent-tree/{treeId}` | 天赋树详情 |
| `GET /talent-tree/{treeId}/playable-specialization/{specId}` | 指定专精的天赋树 |
| `GET /talent/index` | 所有天赋列表 |
| `GET /talent/{talentId}` | 单个天赋详情 |

---

### 成就

| 端点 | 说明 |
|---|---|
| `GET /achievement-category/index` | 成就分类列表 |
| `GET /achievement-category/{categoryId}` | 分类详情（含子分类） |
| `GET /achievement/index` | 所有成就列表 |
| `GET /achievement/{achievementId}` | 成就详情（点数、条件、前置等） |

---

### 副本图鉴（Journal）

| 端点 | 说明 |
|---|---|
| `GET /journal-expansion/index` | 资料片列表 |
| `GET /journal-expansion/{expansionId}` | 资料片详情（含副本列表） |
| `GET /journal-instance/index` | 所有副本（地下城+团队）列表 |
| `GET /journal-instance/{instanceId}` | 副本详情（Boss 列表、难度等） |
| `GET /journal-encounter/index` | 所有 Boss 遭遇列表 |
| `GET /journal-encounter/{encounterId}` | Boss 详情（技能、掉落等） |

---

### 生物

| 端点 | 说明 |
|---|---|
| `GET /creature/{creatureId}` | 生物详情（名称、类型、势力） |
| `GET /creature-family/index` | 生物家族列表（用于猎人宠物） |
| `GET /creature-type/index` | 生物类型列表（人型/野兽/恶魔等） |

---

### 坐骑、宠物、玩具、传家宝

| 端点 | 说明 |
|---|---|
| `GET /mount/index` | 所有坐骑列表 |
| `GET /mount/{mountId}` | 坐骑详情（来源、描述） |
| `GET /pet/index` | 所有战斗宠物列表 |
| `GET /pet/{petId}` | 宠物详情 |
| `GET /pet-ability/index` | 宠物技能列表 |
| `GET /toy/index` | 所有玩具列表 |
| `GET /heirloom/index` | 传家宝列表 |
| `GET /heirloom/{heirloomId}` | 传家宝详情 |

---

### 任务

| 端点 | 说明 |
|---|---|
| `GET /quest/index` | 任务分类入口 |
| `GET /quest/{questId}` | 任务详情（目标、奖励、区域等） |
| `GET /quest/category/index` | 任务分类列表 |
| `GET /quest/area/index` | 任务区域列表 |
| `GET /quest/type/index` | 任务类型列表（日常/周常/主线等） |

---

### 声望

| 端点 | 说明 |
|---|---|
| `GET /reputation-faction/index` | 所有声望势力列表 |
| `GET /reputation-faction/{factionId}` | 势力详情 |
| `GET /reputation-tiers/index` | 声望等级定义（友好/尊敬/崇拜等） |

---

### 其他

| 端点 | 说明 |
|---|---|
| `GET /spell/{spellId}` | 法术详情（名称、描述）。注意：返回的是法术基础信息，不是配方数据 |
| `GET /power-type/index` | 能量类型列表（法力/怒气/能量等） |
| `GET /title/index` | 称号列表 |
| `GET /title/{titleId}` | 称号详情 |
| `GET /guild-crest/index` | 公会徽章素材索引 |

---

## Profile API（需要用户 OAuth）

Base: `https://us.api.blizzard.com/profile`  
Namespace: `profile-us`

用 Client Credentials token 调用这些端点会返回 **403**，必须用用户登录后的 access_token。

### 账号

| 端点 | 说明 |
|---|---|
| `GET /user/wow` | 当前账号下所有 WoW 角色列表（含各版本） |
| `GET /user/wow/protected-character/{realmId}-{characterId}` | 受保护的角色基础信息 |

### 角色

| 端点 | 说明 |
|---|---|
| `GET /wow/character/{realm}/{name}` | 角色总览（等级、种族、职业、成就点数等） |
| `GET /wow/character/{realm}/{name}/achievements` | 角色成就详情 |
| `GET /wow/character/{realm}/{name}/achievements/statistics` | 成就统计数据 |
| `GET /wow/character/{realm}/{name}/equipment` | 当前装备（含每件的 item_id、词条、宝石、附魔） |
| `GET /wow/character/{realm}/{name}/appearance` | 外观（种族、性别、自定义选项） |
| `GET /wow/character/{realm}/{name}/collections` | 收藏总览（坐骑/宠物/玩具/传家宝） |
| `GET /wow/character/{realm}/{name}/collections/mounts` | 已收集坐骑 |
| `GET /wow/character/{realm}/{name}/collections/pets` | 已收集宠物 |
| `GET /wow/character/{realm}/{name}/collections/toys` | 已收集玩具 |
| `GET /wow/character/{realm}/{name}/collections/heirlooms` | 已收集传家宝 |
| `GET /wow/character/{realm}/{name}/encounters` | 副本遭遇记录 |
| `GET /wow/character/{realm}/{name}/encounters/raids` | 团队副本进度 |
| `GET /wow/character/{realm}/{name}/encounters/dungeons` | 地下城记录 |
| `GET /wow/character/{realm}/{name}/professions` | 当前职业技能等级及已学配方 |
| `GET /wow/character/{realm}/{name}/mythic-keystone-profile` | 大秘境档案（当赛季最高评分、历史记录） |
| `GET /wow/character/{realm}/{name}/mythic-keystone-profile/season/{seasonId}` | 指定赛季大秘境记录 |
| `GET /wow/character/{realm}/{name}/pvp-summary` | PvP 总览（段位、胜负场次） |
| `GET /wow/character/{realm}/{name}/pvp-bracket/{bracket}` | 指定段位（2v2/3v3/rbg）详情 |
| `GET /wow/character/{realm}/{name}/quests` | 已完成任务列表 |
| `GET /wow/character/{realm}/{name}/quests/completed` | 已完成任务 ID 列表 |
| `GET /wow/character/{realm}/{name}/reputations` | 声望列表（各势力当前声誉值） |
| `GET /wow/character/{realm}/{name}/soulbinds` | 盟约/灵魂纽带（Shadowlands） |
| `GET /wow/character/{realm}/{name}/specializations` | 当前专精及天赋配置 |
| `GET /wow/character/{realm}/{name}/statistics` | 角色属性面板（攻击力/韧性/急速等） |
| `GET /wow/character/{realm}/{name}/titles` | 已获得称号 |
| `GET /wow/character/{realm}/{name}/hunter-pets` | 猎人宠物列表（仅猎人） |
| `GET /wow/character/{realm}/{name}/media` | 角色头像/背景图片 URL |

### 公会

| 端点 | 说明 |
|---|---|
| `GET /wow/guild/{realm}/{guild}` | 公会信息（名称、成员数、成就点数） |
| `GET /wow/guild/{realm}/{guild}/achievements` | 公会成就 |
| `GET /wow/guild/{realm}/{guild}/roster` | 公会成员名单（含职位） |
| `GET /wow/guild/{realm}/{guild}/activity` | 公会最近动态 |

---

## Search API

支持所有静态数据的搜索，语法统一：

```
GET /data/wow/search/{type}
  ?namespace=static-us
  &{field}.{locale}={value}   // 按字段过滤
  &orderby={field}            // 排序
  &_page={n}                  // 分页（从1开始）
  &_pageSize={n}              // 每页条数（默认100，最大1000）
```

支持搜索的类型：`item`, `creature`, `achievement`, `quest`, `spell`, `mount`, `pet`, `title`, `realm` 等

示例——搜索名字包含 "flask" 的史诗品质物品：
```
GET /data/wow/search/item
  ?namespace=static-us
  &name.en_US=flask
  &quality.type=EPIC
  &_pageSize=50
```

---

## 实际缺陷（亲测）

| 问题 | 描述 |
|---|---|
| `/recipe/{id}` crafted_item 为 null | TWW（至暗之夜）及更新版本所有配方，Blizzard API 均不返回产出物信息，需用 Wowhead 补全 |
| `/spell/{id}/media` 返回 404 | 法术图标无法通过此端点获取 |
| `/achievement/{id}/media` 返回 404 | 成就图标同上 |
| `/item/{id}/media` 返回 404 | 部分物品图标链接失效 |
| Token 不能放 query param | 必须用 `Authorization: Bearer {token}` header，放在 `?access_token=` 会返回 404 |
| Profile API 用 client_credentials 返回 403 | Profile API 必须用户登录 OAuth，不支持 client_credentials |
