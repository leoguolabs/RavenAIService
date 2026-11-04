"""Optional concise provenance guidance, without case facts or another model call."""


def confidence_bounds(locale="zh"):
    if locale == "en":
        return (
            "\n\n## Evidence and confidence bounds (experiment)\n"
            "Keep observed values, material blockers, partial progress and complete outcomes distinct. "
            "An observed listing/state change does not by itself prove an underlying operation or its actor. "
            "Current source describes possible behavior, not verified deployed identity; missing prints do not exclude an actor. "
            "Separate a numeric formula/display format from its external reference, units, timescale, enum domain and runtime environment. "
            "Corrections and conversions require evidence in the project material or original input; do not import an unverified value from another protocol. "
            "A reported/cached value does not independently prove completion. When cause or environment is unverified, answer with the supported observation "
            "and conditional hypotheses instead of exhaustively searching for a definite cause. Only pursue specific gaps that could change the requested findings. "
            "Keep the same qualifications in summary, answer, hypotheses and recommendations. Configuration advice needs a verified mechanism and correct direction. "
            "Preserve all required Skills, native citations, source/attachment checks, public schema and configured tools/turn limit.\n"
        )
    return (
        "\n\n## 证据与置信边界（实验）\n"
        "保留明确观察到的数值、独立阻塞、部分进展与完整终态，并区分其含义。"
        "清单或状态变化本身不能证明底层动作及执行者；当前源码只说明可能路径，不等于已核验的现场部署，缺打印不能排除执行方。"
        "区分数值计算/显示格式与外部参照、单位、时标、枚举域和运行环境；校正量与换算须有项目资料或原始输入支持，不能套用其他协议的未核实数值。"
        "上报值或缓存值不能独立证明过程完成。原因或环境未核实时，用已证实观察和条件假设回答，不必为确定根因而穷尽检索；只继续追查会改变所问结论的具体关键缺口。"
        "摘要、正文、假设和建议使用一致限定；配置建议须以已核实机制为前提并核对作用方向。"
        "必需 Skill、原始引用、源码/附件核验、公开 schema、工具及配置轮数上限均保留。\n"
    )
