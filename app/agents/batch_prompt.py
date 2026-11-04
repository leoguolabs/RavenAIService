"""Optional dependency-aware batching guidance for the original analysis prompt."""


def dependency_batching(locale="zh"):
    if locale == "en":
        return (
            "\n\n## Evidence request grouping (experiment)\n"
            "Group currently known independent evidence requests in one assistant tool-use round. "
            "When several paths or questions are already known, prefer 2–8 independent searches/reads together, "
            "using parallel native tools or one safe combined Bash command with separate labeled results. "
            "A single known request may run immediately; do not invent work to fill a batch. "
            "Respect dependencies: locate an unknown file or finish a repository clone before reading its contents. "
            "After each group, identify the specific unresolved fact that could change the requested answer; "
            "collect those gaps together instead of alternating one tiny search, one tiny read and another search. "
            "Reuse verified results unless a conflict or source change requires rechecking. "
            "Retain per-item native paths/line references, relevant failure branches and full required Skill checks. "
            "Grouping must not substitute an early preview for all matches, omit later/companion evidence, "
            "truncate a needed function or infer an unobserved outcome. Preserve tools, schema and turn limit.\n"
        )
    return (
        "\n\n## 证据请求分组（实验）\n"
        "把当前已知、互不依赖的证据请求放在同一轮工具调用中。已知多个路径或检查点时，优先一次合并2–8项独立检索/读取，"
        "可同轮并行使用原生工具，或用一条安全组合的Bash命令并分项标注结果。只有一个已知请求时直接执行，不为凑批次增加工作。"
        "遵守依赖：路径未知先定位，仓库克隆完成后才读取其中内容。每批返回后，先指出仍会改变所问结论的具体事实缺口，"
        "把这些缺口合并补查，避免一次小检索、一次小读取、再一次小检索地交替推进。已核实结果直接复用，除非出现冲突或源文件变化。"
        "每项保留原始路径和行号，核验相关失败分支及全部必需Skill要求。分组不能以首屏替代全量匹配，不能丢掉较晚或伴随日志证据，"
        "不能截断所需函数或推断未观察到的终态；工具、schema及轮数上限不变。\n"
    )
