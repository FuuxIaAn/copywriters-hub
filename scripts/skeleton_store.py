# -*- coding: utf-8 -*-
"""
文案骨架库存储模块

每套骨架模板包含：
- name: 模板名称
- scene: 适用场景（情感/事业/财运/避坑/干货...）
- structure_type: 结构类型（痛点型/案例型/反差型/三段论...）
- core_structure: 核心结构（段序 + 段功能 + 篇幅配比）
- emotion_curve: 情绪曲线描述
- hook_types: 钩子类型
- cta_positions: 促动按钮位置
- variation_rules: 变奏规则
- case_example: 实战案例
- effect_feedback: 效果回填
- compliance_level: 合规等级
- created_at / updated_at
"""
import json
import os
import threading
from datetime import datetime

_DEFAULT_TEMPLATES = [
    {
        "id": "tpl_001",
        "name": "痛点共鸣型",
        "scene": "情感",
        "structure_type": "痛点型",
        "core_structure": "钩子(15%) → 痛点共鸣(30%) → 颠覆认知(25%) → 方案输出(20%) → 促动收尾(10%)",
        "emotion_curve": "刺痛 → 共鸣 → 好奇 → 释然 → 行动",
        "hook_types": "人群拦截型 / 灵魂拷问型",
        "cta_positions": "结尾CTA + 评论互动钩子",
        "variation_rules": "干货类可压缩共鸣段扩方案段；故事类可扩痛点段加细节",
        "case_example": "「你是不是也经常这样...」开头拦截 → 说中痛点 → 给出反直觉解法 → 落地步骤 → 引导评论",
        "effect_feedback": "",
        "compliance_level": "弱承诺+强共鸣",
        "created_at": "2026-08-13",
        "updated_at": "2026-08-13",
    },
    {
        "id": "tpl_002",
        "name": "反差冲击型",
        "scene": "事业",
        "structure_type": "反差型",
        "core_structure": "反差钩子(10%) → 铺垫背景(25%) → 反转揭秘(35%) → 方法论(20%) → 促动(10%)",
        "emotion_curve": "惊讶 → 好奇 → 恍然 → 信服 → 行动",
        "hook_types": "冲突引爆型 / 数据反差型",
        "cta_positions": "关注引导 + 私域引流",
        "variation_rules": "强反差可前置爆点；弱反差需加悬念铺垫",
        "case_example": "「月入3千到3万我只做了一件事」→ 背景 → 关键转折 → 具体方法 → 关注看续集",
        "effect_feedback": "",
        "compliance_level": "数据需核查",
        "created_at": "2026-08-13",
        "updated_at": "2026-08-13",
    },
    {
        "id": "tpl_003",
        "name": "避坑指南型",
        "scene": "避坑",
        "structure_type": "清单型",
        "core_structure": "恐惧钩子(10%) → 坑1(20%) → 坑2(20%) → 坑3(20%) → 正确做法(20%) → 促动(10%)",
        "emotion_curve": "焦虑 → 紧张 → 紧张 → 释然 → 感恩 → 行动",
        "hook_types": "损失规避型 / 灵魂拷问型",
        "cta_positions": "收藏引导 + 转发提醒",
        "variation_rules": "坑的数量可3-5个；每个坑控制在15秒内",
        "case_example": "「这5个坑踩了就晚了」→ 逐个拆解 → 正确做法 → 引导收藏",
        "effect_feedback": "",
        "compliance_level": "弱承诺+强共鸣",
        "created_at": "2026-08-13",
        "updated_at": "2026-08-13",
    },
    {
        "id": "tpl_004",
        "name": "故事叙事型",
        "scene": "故事",
        "structure_type": "故事型",
        "core_structure": "悬念钩子(10%) → 人物背景(15%) → 冲突升级(35%) → 转折解法(25%) → 金句收尾(15%)",
        "emotion_curve": "好奇 → 代入 → 紧张 → 释然 → 共鸣",
        "hook_types": "悬念型 / 场景代入型",
        "cta_positions": "评论区讲你的故事 + 关注看续集",
        "variation_rules": "真实经历增强信任；虚构故事需标注演绎",
        "case_example": "「三年前我欠了80万，今天把账本摊开给你看」→ 背景 → 关键转折 → 方法论 → 金句",
        "effect_feedback": "",
        "compliance_level": "弱承诺+强共鸣",
        "created_at": "2026-08-14",
        "updated_at": "2026-08-14",
    },
    {
        "id": "tpl_005",
        "name": "干货清单型",
        "scene": "干货",
        "structure_type": "清单型",
        "core_structure": "结果钩子(10%) → 痛点确认(15%) → 清单项1-5(50%) → 避坑提醒(15%) → 促动(10%)",
        "emotion_curve": "好奇 → 认同 → 收获 → 警惕 → 行动",
        "hook_types": "结果前置型 / 数字承诺型",
        "cta_positions": "收藏引导 + 主页看更多",
        "variation_rules": "清单项控制在3-7条；每条配一句人话解释",
        "case_example": "「做账号第1年，我靠这5个清单多赚了20万」→ 逐条讲清 → 提醒踩坑 → 引导收藏",
        "effect_feedback": "",
        "compliance_level": "数据需核查",
        "created_at": "2026-08-14",
        "updated_at": "2026-08-14",
    },
    {
        "id": "tpl_006",
        "name": "测评对比型",
        "scene": "测评",
        "structure_type": "对比型",
        "core_structure": "选择困境钩子(10%) → 测评维度(15%) → A/B对比(45%) → 结论推荐(20%) → 避坑提示(10%)",
        "emotion_curve": "纠结 → 清晰 → 信服 → 安心 → 行动",
        "hook_types": "选择困难型 / 避坑型",
        "cta_positions": "评论区提问 + 主页合集",
        "variation_rules": "对比维度要具体；避免一边倒尬吹",
        "case_example": "「200元和2000元的麦克风，差别到底在哪？」→ 维度 → 对比 → 结论 → 提醒",
        "effect_feedback": "",
        "compliance_level": "客观中立+数据来源",
        "created_at": "2026-08-14",
        "updated_at": "2026-08-14",
    },
    {
        "id": "tpl_007",
        "name": "争议引爆型",
        "scene": "争议",
        "structure_type": "争议型",
        "core_structure": "反常识观点(15%) → 正方理由(20%) → 反方理由(20%) → 我的立场(30%) → 邀请讨论(15%)",
        "emotion_curve": "惊讶 → 对抗 → 思考 → 认同/反对 → 表达",
        "hook_types": "反常识型 / 挑战共识型",
        "cta_positions": "评论区站队 + 下期展开",
        "variation_rules": "观点必须有依据；不要为争议而争议",
        "case_example": "「我不建议普通人做短视频」→ 先说理由 → 承认反方 → 给出边界 → 邀请讨论",
        "effect_feedback": "",
        "compliance_level": "观点需自洽+避免引战",
        "created_at": "2026-08-14",
        "updated_at": "2026-08-14",
    },
    {
        "id": "tpl_008",
        "name": "逆袭成长型",
        "scene": "成长",
        "structure_type": "逆袭型",
        "core_structure": "低谷钩子(10%) → 至暗时刻(25%) → 关键转折(25%) → 成长方法(25%) → 激励收尾(15%)",
        "emotion_curve": "压抑 → 共鸣 → 希望 → 振奋 → 行动",
        "hook_types": "前后对比型 / 身份反差型",
        "cta_positions": "关注见证成长 + 评论区立flag",
        "variation_rules": "转折要具体；方法要可复制",
        "case_example": "「从被辞退到月入5万，我只改了这3个习惯」→ 低谷 → 转折 → 方法 → 激励",
        "effect_feedback": "",
        "compliance_level": "数据需核查",
        "created_at": "2026-08-14",
        "updated_at": "2026-08-14",
    },
    {
        "id": "tpl_009",
        "name": "金句观点型",
        "scene": "观点",
        "structure_type": "金句型",
        "core_structure": "金句钩子(20%) → 观点阐释(25%) → 案例佐证(30%) → 反向论证(15%) → 金句收尾(10%)",
        "emotion_curve": "触动 → 思考 → 认同 → 警醒 → 共鸣",
        "hook_types": "金句型 / 哲理型",
        "cta_positions": "转发金句 + 评论区写下你的版本",
        "variation_rules": "金句要口语化；案例要贴近受众",
        "case_example": "「真正废掉一个人的，不是懒，而是总在做紧急但不重要的事」→ 阐释 → 案例 → 反向 → 金句",
        "effect_feedback": "",
        "compliance_level": "弱承诺+强共鸣",
        "created_at": "2026-08-14",
        "updated_at": "2026-08-14",
    },
    {
        "id": "tpl_010",
        "name": "提问互动型",
        "scene": "互动",
        "structure_type": "问答型",
        "core_structure": "提问钩子(15%) → 用户答案预判(15%) → 揭晓答案(30%) → 延伸方法(25%) → 再提问收尾(15%)",
        "emotion_curve": "好奇 → 参与 → 恍然大悟 → 收获 → 表达",
        "hook_types": "互动提问型 / 测试型",
        "cta_positions": "评论区回答 + 下期揭晓",
        "variation_rules": "问题要简单；答案要反直觉",
        "case_example": "「你猜做口播最大的成本是什么？不是设备，是这条」→ 预判 → 揭晓 → 方法 → 再问",
        "effect_feedback": "",
        "compliance_level": "弱承诺+强共鸣",
        "created_at": "2026-08-14",
        "updated_at": "2026-08-14",
    },
]


def _skeleton_path(output_dir: str) -> str:
    return os.path.join(output_dir, "skeletons.json")


def _load(output_dir: str) -> dict:
    path = _skeleton_path(output_dir)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"[skeleton] 读取骨架库失败: {e}")
    # 首次使用，写入默认模板
    data = {"templates": list(_DEFAULT_TEMPLATES), "version": 1}
    _save(output_dir, data)
    return data


def ensure_defaults(output_dir: str):
    """把新增默认模板补充到现有骨架库（不覆盖已有模板）。"""
    data = _load(output_dir)
    existing_ids = {t.get("id") for t in data.get("templates", [])}
    added = 0
    for t in _DEFAULT_TEMPLATES:
        if t.get("id") not in existing_ids:
            data.setdefault("templates", []).append(dict(t))
            added += 1
    if added:
        data["version"] = data.get("version", 1) + 1
        _save(output_dir, data)
        print(f"[skeleton] 已补充 {added} 套新默认模板")
    return added


_LOCK = threading.Lock()


def _save(output_dir: str, data: dict):
    path = _skeleton_path(output_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def list_templates(output_dir: str) -> list:
    """返回全部骨架模板。"""
    data = _load(output_dir)
    return data.get("templates", [])


def add_template(output_dir: str, template: dict) -> dict:
    """新增骨架模板。"""
    with _LOCK:
        data = _load(output_dir)
        tid = "tpl_" + datetime.now().strftime("%Y%m%d%H%M%S")
        template["id"] = tid
        template["created_at"] = datetime.now().strftime("%Y-%m-%d")
        template["updated_at"] = template["created_at"]
        data.setdefault("templates", []).append(template)
        _save(output_dir, data)
        return template


def update_template(output_dir: str, tid: str, updates: dict) -> dict | None:
    """更新指定骨架模板。"""
    with _LOCK:
        data = _load(output_dir)
        for t in data.get("templates", []):
            if t.get("id") == tid:
                t.update(updates)
                t["updated_at"] = datetime.now().strftime("%Y-%m-%d")
                _save(output_dir, data)
                return t
        return None


def delete_template(output_dir: str, tid: str) -> bool:
    """删除指定骨架模板。"""
    with _LOCK:
        data = _load(output_dir)
        before = len(data.get("templates", []))
        data["templates"] = [t for t in data.get("templates", []) if t.get("id") != tid]
        after = len(data["templates"])
        if after < before:
            _save(output_dir, data)
            return True
        return False


def match_templates(output_dir: str, script: str, top_n: int = 3) -> list:
    """根据文稿内容匹配最相关的骨架模板（简单关键词匹配）。

    匹配逻辑：
    1. 提取文稿关键词
    2. 与模板的 scene / structure_type / name / case_example 做匹配
    3. 返回匹配度最高的 top_n 个模板
    """
    templates = list_templates(output_dir)
    if not templates:
        return []

    scored = []
    for t in templates:
        score = 0
        # 场景匹配
        scene = t.get("scene", "")
        if scene and scene in script:
            score += 3
        # 结构类型关键词
        stype = t.get("structure_type", "")
        if stype:
            for kw in stype.replace("型", "").split("/"):
                kw = kw.strip()
                if kw and kw in script:
                    score += 2
        # 模板名关键词
        for kw in t.get("name", ""):
            if kw in script:
                score += 1
        # 案例关键词
        case = t.get("case_example", "")
        if case:
            for kw in ["痛点", "共鸣", "反差", "避坑", "钩子", "促动"]:
                if kw in case and kw in script:
                    score += 1
        scored.append((score, t))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored[:top_n] if _ > 0] or templates[:top_n]


def templates_text(output_dir: str, top_n: int = 5) -> str:
    """返回骨架库摘要文本，用于注入 agent prompt。"""
    templates = list_templates(output_dir)[:top_n]
    if not templates:
        return ""
    lines = []
    for t in templates:
        lines.append(
            f"【{t['name']}】场景:{t.get('scene','')} 结构:{t.get('structure_type','')}\n"
            f"  配比: {t.get('core_structure','')}\n"
            f"  情绪: {t.get('emotion_curve','')}\n"
            f"  钩子: {t.get('hook_types','')}\n"
            f"  促动: {t.get('cta_positions','')}\n"
            f"  变奏: {t.get('variation_rules','')}"
        )
    return "\n".join(lines)
