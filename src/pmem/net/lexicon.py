"""T0 词表：地名 / 组织 / 工具 / 领域概念（03 §8.1）。

为什么用内置词表而不是模型：M3 的定位是 **T0 规则层**——不花 token、可解释、
可单测。词表的覆盖度决定关系抽取的召回，抽不到的东西**宁可不建边**，
也不要造出一个类型不明的实体（那会污染 2 跳查询和可信度）。

词表里只放"有明确类型"的词：城市、公司、软件、常见爱好/领域。
人名不查表——中文人名没有封闭集合，改用结构模板（见 ``entities.py``）。
"""

from __future__ import annotations

CITIES: frozenset[str] = frozenset(
    """
    北京 上海 天津 重庆 广州 深圳 杭州 南京 苏州 无锡 常州 南通 徐州 扬州 镇江
    宁波 温州 绍兴 嘉兴 湖州 金华 台州 合肥 芜湖 蚌埠 福州 厦门 泉州 漳州 南昌
    赣州 济南 青岛 烟台 潍坊 淄博 临沂 郑州 洛阳 开封 新乡 武汉 宜昌 襄阳 长沙
    株洲 湘潭 岳阳 广州 珠海 佛山 东莞 中山 惠州 汕头 南宁 桂林 柳州 海口 三亚
    成都 绵阳 德阳 贵阳 遵义 昆明 大理 丽江 拉萨 西安 咸阳 宝鸡 兰州 银川 西宁
    乌鲁木齐 呼和浩特 太原 大同 石家庄 唐山 保定 廊坊 沈阳 大连 鞍山 长春 吉林
    哈尔滨 大庆 齐齐哈尔 香港 澳门 台北 高雄 台中 台南 新竹
    新加坡 东京 大阪 京都 首尔 釜山 曼谷 吉隆坡 悉尼 墨尔本 伦敦 巴黎 柏林
    慕尼黑 阿姆斯特丹 苏黎世 纽约 旧金山 西雅图 洛杉矶 波士顿 多伦多 温哥华
    中国 美国 英国 法国 德国 日本 韩国 加拿大 澳大利亚 新西兰 荷兰 瑞士 瑞典
    挪威 丹麦 芬兰 意大利 西班牙 葡萄牙 俄罗斯 印度 巴西 阿根廷 墨西哥 埃及
    浙江 江苏 广东 山东 河南 河北 湖南 湖北 四川 云南 贵州 陕西 甘肃 青海 福建
    江西 安徽 山西 辽宁 吉林 黑龙江 海南 台湾 新疆 西藏 宁夏 广西 内蒙古
    """.split()
)

ORGS: frozenset[str] = frozenset(
    """
    字节 字节跳动 小米 美团 京东 网易 腾讯 阿里 阿里巴巴 百度 华为 快手 滴滴
    拼多多 携程 蔚来 小鹏 理想 大疆 商汤 旷视 依图 云从 小红书 知乎 豆瓣 迅雷
    同程 哔哩哔哩 B站 微博 搜狐 新浪 360 奇虎 微软 谷歌 Google 苹果 Apple
    亚马逊 Amazon 英伟达 NVIDIA 英特尔 Intel 高通 三星 索尼 任天堂 特斯拉 Tesla
    OpenAI Anthropic DeepSeek 深度求索 月之暗面 智谱 MiniMax 百川 零一万物
    阶跃星辰 商汤科技 旷视科技 中科院 清华 北京大学 浙江大学 复旦大学 上海交大
    中国银行 工商银行 建设银行 农业银行 招商银行 平安 中信 高盛 摩根
    """.split()
)

TOOLS: frozenset[str] = frozenset(
    """
    Python Rust Go Java Kotlin Swift TypeScript JavaScript C++ C# Ruby PHP Scala
    Haskell Elixir Erlang Lua R Julia MATLAB SQL
    Docker Kubernetes Podman Nginx Redis Postgres PostgreSQL MySQL SQLite MongoDB
    Kafka RabbitMQ Elasticsearch ClickHouse DuckDB
    Linux Windows macOS Ubuntu Debian Arch
    Git GitHub GitLab SVN Vim Neovim Emacs VSCode Zed Raycast Alfred
    Figma Notion Obsidian Logseq Anki Excel Word PowerPoint
    PyTorch TensorFlow JAX scikit-learn Pandas NumPy
    React Vue Svelte Next.js Node.js Deno Bun
    Flutter Android iOS
    DSH Harness Claude Codex Cursor Copilot
    """.split()
)

CONCEPTS: frozenset[str] = frozenset(
    """
    咖啡 茶 酒 威士忌 红酒 拉面 火锅 烧烤 寿司 川菜 湘菜 粤菜 甜点 烘焙 做饭
    徒步 露营 爬山 骑行 摄影 围棋 国际象棋 游泳 滑雪 潜水 冲浪 跑步 健身 瑜伽
    吉他 钢琴 小提琴 书法 画画 手冲 养鱼 种花 养猫 养狗 旅行 阅读 写作 看电影
    科幻 推理 历史 哲学 心理学 经济学 数学 物理 化学 生物 天文 医学
    编程 开源 算法 数据结构 数据库 分布式 系统设计 编译器 操作系统 网络 安全
    机器学习 深度学习 强化学习 大模型 向量检索 知识图谱 记忆系统 认知科学
    产品设计 交互设计 用户体验 商业模式 创业 投资 理财 股票 基金 加密货币
    游戏 独立游戏 桌游 剧本杀 动漫 漫画 音乐 摇滚 爵士 古典乐 电子音乐
    AI 人工智能 可塑性 记忆 睡眠 反思 进化 特质
    工资 实习 外卖 电车 多肉 拿铁 美式 拉花 通勤 加班 辞职 相亲 结婚
    """.split()
)

# 人名的结构模板需要排除的高频虚词/时间词：它们出现在"在…工作"前面时
# 会被误当成人名（实测踩过："现在在旷视工作" → 抽出"现在"）。
PERSON_STOPWORDS: frozenset[str] = frozenset(
    """
    现在 目前 最近 以前 之前 以后 之后 今天 昨天 明天 后天 今年 去年 明年
    我们 你们 他们 她们 它们 大家 自己 别人 有人 这个 那个 这些 那些 这里 那里
    因为 所以 但是 而且 不过 然后 其实 已经 一直 平时 通常 经常 偶尔 早上 晚上
    中午 下午 上午 周末 工作日 小时候 长大后 后来 当时 那时 这时 同时 此外
    如果 虽然 即使 只要 除非 无论 不管 还是 或者 并且 何况 反而 居然 果然
    不是 就是 但是 只是 于是 可是 而是 需要 应该 可以 可能 大概 直接
    希望 感觉 觉得 知道 认为 看到 听到 说完 开始 继续 然后 而且 不过
    公司 学校 单位 家里 国外 国内 一线 二线 三线 城市 地方 东西 事情 时候
    用户 主人 自己 大家 有人 别人 朋友 同事 同学 老师 老板 领导 家人
    私信 艾特 评论 回复 视频 会话 消息 语音 电话 图片 文件 代码 项目 阶段
    功能 问题 需求 方案 版本 效果 结果 内容 信息 数据 系统 模型 记忆 工具
    """.split()
)

TYPE_BY_TERM: dict[str, str] = {}
for _term in CITIES:
    TYPE_BY_TERM[_term] = "place"
for _term in ORGS:
    TYPE_BY_TERM[_term] = "org"
for _term in TOOLS:
    TYPE_BY_TERM[_term] = "tool"
for _term in CONCEPTS:
    TYPE_BY_TERM[_term] = "concept"

# 长词优先匹配：先匹配"字节跳动"，再匹配"字节"
TERMS_BY_LENGTH: tuple[str, ...] = tuple(sorted(TYPE_BY_TERM, key=len, reverse=True))


def type_of(term: str) -> str | None:
    return TYPE_BY_TERM.get(term)


def all_terms() -> tuple[str, ...]:
    return TERMS_BY_LENGTH
