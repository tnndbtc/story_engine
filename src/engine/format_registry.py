"""
Format registry — maps format IDs (10-46) to selector strategies and prompt files.

Selector strategies:
  'single'      — 1 item from get_top_items (reuses explainer logic)
  'mix'         — N items from _select_by_mix (category-balanced)
  'comment'     — N items, preferring platforms with comments
  'topic_match' — N items, LLM identifies topic overlaps

Format 1-9 use dedicated selector/generator functions (legacy).
Format 10-46 use generic select_for_format() + generate_by_format().
"""

# (selector_strategy, prompt_file, item_count)
FORMAT_REGISTRY: dict[int, tuple[str, str, int]] = {
    10: ('single',      'format_10.txt', 1),    # 反直觉
    11: ('single',      'format_11.txt', 1),    # 角色代入
    12: ('mix',         'format_12.txt', 5),    # 时间线复盘
    13: ('mix',         'format_13.txt', 5),    # 谁赢谁输
    14: ('mix',         'format_14.txt', 3),    # 关键数据
    15: ('topic_match', 'format_15.txt', 3),    # 谣言 vs 真相
    16: ('single',      'format_16.txt', 1),    # 被忽视但重要
    17: ('single',      'format_17.txt', 1),    # 背景补课
    18: ('single',      'format_18.txt', 1),    # 二选一
    19: ('single',      'format_19.txt', 1),    # 未来会怎样
    20: ('single',      'format_20.txt', 1),    # 一句话总结
    21: ('single',      'format_21.txt', 1),    # 最离谱新闻
    22: ('mix',         'format_22.txt', 5),    # 同类对比
    23: ('mix',         'format_23.txt', 5),    # 排行榜
    24: ('mix',         'format_24.txt', 3),    # 错误决策
    25: ('single',      'format_25.txt', 1),    # 连锁反应
    26: ('comment',     'format_26.txt', 3),    # 情绪解读
    27: ('single',      'format_27.txt', 1),    # 第一视角叙述
    28: ('single',      'format_28.txt', 1),    # 极端假设
    29: ('single',      'format_29.txt', 1),    # 一分钟故事版
    30: ('mix',         'format_30.txt', 5),    # 黑白对立
    31: ('comment',     'format_31.txt', 5),    # 热门评论精选
    32: ('topic_match', 'format_32.txt', 5),    # 误判合集
    33: ('single',      'format_33.txt', 1),    # 关键词拆解
    34: ('mix',         'format_34.txt', 10),   # 24小时回顾
    35: ('topic_match', 'format_35.txt', 5),    # 不同标题对比
    36: ('single',      'format_36.txt', 1),    # 冷知识关联
    37: ('single',      'format_37.txt', 1),    # 幕后逻辑
    38: ('mix',         'format_38.txt', 3),    # 失败案例
    39: ('mix',         'format_39.txt', 3),    # 成功路径
    40: ('mix',         'format_40.txt', 3),    # 三点结论
    41: ('mix',         'format_41.txt', 3),    # 你需要知道的
    42: ('single',      'format_42.txt', 1),    # 误区提醒
    43: ('single',      'format_43.txt', 1),    # 对普通人的影响
    44: ('single',      'format_44.txt', 1),    # 短问短答
    45: ('single',      'format_45.txt', 1),    # 单一概念解释
    46: ('single',      'format_46.txt', 1),    # 历史对照
}

# Human-readable names for display
FORMAT_NAMES: dict[int, str] = {
    10: '反直觉', 11: '角色代入', 12: '时间线复盘', 13: '谁赢谁输',
    14: '关键数据', 15: '谣言vs真相', 16: '被忽视但重要', 17: '背景补课',
    18: '二选一', 19: '未来会怎样', 20: '一句话总结', 21: '最离谱新闻',
    22: '同类对比', 23: '排行榜', 24: '错误决策', 25: '连锁反应',
    26: '情绪解读', 27: '第一视角叙述', 28: '极端假设', 29: '一分钟故事版',
    30: '黑白对立', 31: '热门评论精选', 32: '误判合集', 33: '关键词拆解',
    34: '24小时回顾', 35: '不同标题对比', 36: '冷知识关联', 37: '幕后逻辑',
    38: '失败案例', 39: '成功路径', 40: '三点结论', 41: '你需要知道的',
    42: '误区提醒', 43: '对普通人的影响', 44: '短问短答', 45: '单一概念解释',
    46: '历史对照',
}
