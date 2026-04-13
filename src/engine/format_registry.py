"""
Format registry — maps format IDs (1-46) to selector strategies and prompt files.

Selector strategies:
  'single'      — 1 item from get_top_items (reuses explainer logic)
  'mix'         — N items from _select_by_mix (category-balanced)
  'comment'     — N items, preferring platforms with comments
  'topic_match' — N items, LLM identifies topic overlaps

Formats 1-9 are the legacy named formats.
Formats 10-46 use generic select_for_format() + generate_by_format().

FORMAT_REQUIRES_NEWS has been removed. Format eligibility rules (which
formats require news events, which require comment sources) are now
configured in story_mix.json under the format_eligibility key.
"""

# (selector_strategy, prompt_file, item_count, context_item_count) — all 46 formats
FORMAT_REGISTRY: dict[int, tuple[str, str, int, int]] = {
    # Legacy formats 1–9
    1:  ('mix',         'explainer.txt',    1,  2),    # explainer
    2:  ('mix',         'top5.txt',         5,  0),    # top5
    3:  ('mix',         'radar.txt',        5,  0),    # radar
    4:  ('mix',         'regional.txt',     5,  2),    # regional
    5:  ('mix',         'two_takes.txt',    8,  0),    # two_takes
    6:  ('mix',         'pattern.txt',     12,  0),    # pattern
    7:  ('mix',         'viral.txt',        5,  2),    # viral
    8:  ('mix',         'deep_dive.txt',   15,  3),    # deep_dive
    9:  ('mix',         'niche.txt',        5,  2),    # niche
    # Extended formats 10–46
    10: ('single',      'format_10.txt',    1,  2),    # 反直觉
    11: ('single',      'format_11.txt',    1,  0),    # 角色代入
    12: ('mix',         'format_12.txt',    5,  2),    # 时间线复盘
    13: ('mix',         'format_13.txt',    5,  0),    # 谁赢谁输
    14: ('mix',         'format_14.txt',    3,  2),    # 关键数据
    15: ('topic_match', 'format_15.txt',    3,  2),    # 谣言 vs 真相
    16: ('single',      'format_16.txt',    1,  0),    # 被忽视但重要
    17: ('single',      'format_17.txt',    1,  2),    # 背景补课
    18: ('single',      'format_18.txt',    1,  0),    # 二选一
    19: ('single',      'format_19.txt',    1,  2),    # 未来会怎样
    20: ('single',      'format_20.txt',    1,  0),    # 一句话总结
    21: ('single',      'format_21.txt',    1,  0),    # 最离谱新闻
    22: ('mix',         'format_22.txt',    5,  0),    # 同类对比
    23: ('mix',         'format_23.txt',    5,  0),    # 排行榜
    24: ('mix',         'format_24.txt',    3,  2),    # 错误决策
    25: ('single',      'format_25.txt',    1,  2),    # 连锁反应
    26: ('comment',     'format_26.txt',    3,  0),    # 情绪解读
    27: ('single',      'format_27.txt',    1,  0),    # 第一视角叙述
    28: ('single',      'format_28.txt',    1,  0),    # 极端假设
    29: ('single',      'format_29.txt',    1,  0),    # 一分钟故事版
    30: ('mix',         'format_30.txt',    5,  0),    # 黑白对立
    31: ('comment',     'format_31.txt',    5,  0),    # 热门评论精选
    32: ('topic_match', 'format_32.txt',    5,  0),    # 误判合集
    33: ('single',      'format_33.txt',    1,  2),    # 关键词拆解
    34: ('mix',         'format_34.txt',   10,  0),    # 24小时回顾
    35: ('topic_match', 'format_35.txt',    5,  0),    # 不同标题对比
    36: ('single',      'format_36.txt',    1,  2),    # 冷知识关联
    37: ('single',      'format_37.txt',    1,  2),    # 幕后逻辑
    38: ('mix',         'format_38.txt',    3,  2),    # 失败案例
    39: ('mix',         'format_39.txt',    3,  2),    # 成功路径
    40: ('mix',         'format_40.txt',    3,  0),    # 三点结论
    41: ('mix',         'format_41.txt',    3,  0),    # 你需要知道的
    42: ('single',      'format_42.txt',    1,  2),    # 误区提醒
    43: ('single',      'format_43.txt',    1,  2),    # 对普通人的影响
    44: ('single',      'format_44.txt',    1,  0),    # 短问短答
    45: ('single',      'format_45.txt',    1,  2),    # 单一概念解释
    46: ('single',      'format_46.txt',    1,  2),    # 历史对照
}

# Explicit split dicts — the new selector uses these directly
FORMAT_STRATEGIES: dict[int, str]  = {fid: v[0] for fid, v in FORMAT_REGISTRY.items()}
FORMAT_ITEM_COUNTS: dict[int, int]  = {fid: v[2] for fid, v in FORMAT_REGISTRY.items()}
FORMAT_CONTEXT_COUNTS: dict[int, int] = {fid: v[3] for fid, v in FORMAT_REGISTRY.items()}

# Reverse map: CLI string name → int format_id
FORMAT_NAME_TO_ID: dict[str, int] = {
    'explainer': 1,
    'top5':      2,
    'radar':     3,
    'regional':  4,
    'two_takes': 5,
    'pattern':   6,
    'viral':     7,
    'deep_dive': 8,
    'niche':     9,
}

# Human-readable names for display (extended formats only — legacy use string names)
FORMAT_NAMES: dict[int, str] = {
    1:  'explainer',       2:  'top5',            3:  'radar',
    4:  'regional',        5:  'two_takes',        6:  'pattern',
    7:  'viral',           8:  'deep_dive',        9:  'niche',
    10: '反直觉',          11: '角色代入',         12: '时间线复盘',
    13: '谁赢谁输',        14: '关键数据',         15: '谣言vs真相',
    16: '被忽视但重要',    17: '背景补课',         18: '二选一',
    19: '未来会怎样',      20: '一句话总结',        21: '最离谱新闻',
    22: '同类对比',        23: '排行榜',           24: '错误决策',
    25: '连锁反应',        26: '情绪解读',         27: '第一视角叙述',
    28: '极端假设',        29: '一分钟故事版',     30: '黑白对立',
    31: '热门评论精选',    32: '误判合集',         33: '关键词拆解',
    34: '24小时回顾',      35: '不同标题对比',     36: '冷知识关联',
    37: '幕后逻辑',        38: '失败案例',         39: '成功路径',
    40: '三点结论',        41: '你需要知道的',     42: '误区提醒',
    43: '对普通人的影响',  44: '短问短答',         45: '单一概念解释',
    46: '历史对照',
}


def item_count(format_id: int) -> int:
    """Return the required item count for a format. Raises KeyError if unknown."""
    return FORMAT_ITEM_COUNTS[format_id]


def strategy(format_id: int) -> str:
    """Return the selector strategy name for a format. Raises KeyError if unknown."""
    return FORMAT_STRATEGIES[format_id]


def context_item_count(format_id: int) -> int:
    """Return the number of background context articles to fetch for this format."""
    return FORMAT_CONTEXT_COUNTS.get(format_id, 0)
