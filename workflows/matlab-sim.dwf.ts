/* zcode-workflow
description: 拆解 PDF 课件中的仿真题，逐题走
  规划→编码→lint门禁→MATLAB运行→独立评分（跑通40/公式40/图表20）的返工循环。拆题用 pdf-courseware
  skill（pymupdf 渲染页面图像，首次自动装依赖）+ 分片读题员并行（每人≤12页）+ 整合员去重。控制系统/动力学类题优先 Simulink
  建模（脚本化 new_system/add_block 搭模后 sim 运行，同一个 sim.m
  自包含），只有很简单的问题才直接写脚本。产物（.m脚本+.slx模型+图）存桌面 matlab-sim 目录。执行通道走 mcp__matlab__
  插件（常驻会话）。
whenToUse: 用户提供 PDF 课件或题目集、需要逐题 MATLAB/Simulink 仿真并按分数验收时。依赖 pdf-courseware
  skill（拆题渲染工具；pdfTool 参数指定其 pdf_tools.py 绝对路径，留空则按 skill 说明自动定位）。
args:
  batch:
    type: number
    description: 同时推进的题数
    default: 2
  courseware:
    type: string
    description: PDF 课件的绝对路径
    required: true
  formulaFloor:
    type: number
    description: 『结果符合公式预期』档（满分40）的最低分
    default: 32
  maxRounds:
    type: number
    description: 每题最大返工轮数
    default: 5
  outputBase:
    type: string
    description: 产物根目录（每次运行在其下自动建带时间戳的子文件夹；~ 开头会解析为用户主目录）
    default: ~/Desktop/matlab-sim
  pdfTool:
    type: string
    description: pdf-courseware skill 里 pdf_tools.py 的绝对路径；留空则运行时按 skill 说明自动定位
  threshold:
    type: number
    description: 过线总分下限
    default: 85
*/
// 定稿参数，调用时可覆盖
const courseware = String(args.courseware ?? "");
const outputBase = String(args.outputBase ?? "~/Desktop/matlab-sim");
const pdfToolArg = String(args.pdfTool ?? "");
const threshold = Number(args.threshold ?? 85);
const formulaFloor = Number(args.formulaFloor ?? 32);
const maxRounds = Number(args.maxRounds ?? 5);
const batchSize = Number(args.batch ?? 2);
const codeTries = 2; // 每轮内 编码⇄审查 的重试上限，语法问题不烧返工轮

interface Problem {
  /** 题号，如 "1"、"3.2"；也是结果子目录名 */
  id: string;
  /** 一句话题目标题 */
  title: string;
  /** 题目完整描述：全部已知条件与要求求解的内容 */
  description: string;
  /** 题面涉及的关键公式（LaTeX 或文字），没有则空串 */
  formulas: string;
  /** 题目在课件中的页码，如 "p.12" 或 "p.12-13" */
  sourcePage: string;
}

interface Plan {
  /** 仿真途径。控制系统、动力学、框图类问题必须用 "simulink" 建模仿真；只有很简单的问题（纯数值计算、简单绘图、无系统结构）才用 "script" */
  approach: "simulink" | "script";
  /** approach=simulink 时：模型设计（模块清单、连线、输入输出端口、求解器与仿真时长）；approach=script 时写"无" */
  modelDesign: string;
  /** 仿真目标，两三句话 */
  objective: string;
  /** 公式推导与数值方案：建模思路、理论值怎么算 */
  derivation: string;
  /** 运行后应存在哪些关键变量（名称与物理含义），供执行员收集、评分员核对 */
  expectedVariables: string;
  /** 应输出哪些图，每图一行：内容与坐标轴 */
  figurePlan: string;
}

interface CodeResult {
  /** 保存的 .m 脚本绝对路径（simulink 途径时该脚本同时负责搭模） */
  path: string;
  /** 脚本做什么，两句话；simulink 途径时说明搭了哪些模块 */
  summary: string;
}

interface ExecResult {
  /** lint_script 报出的语法错误（含行号）；干净则为空数组 */
  lintErrors: string[];
  /** 仅当脚本完整跑完才为 true */
  ran: boolean;
  /** 控制台输出的末尾（截到最后2000字符），出错时为错误原文 */
  consoleOutput: string;
  /** 导出的 PNG 图绝对路径 */
  figures: string[];
  /** 收集到的关键变量：名 → 数值/摘要 */
  variables: { name: string; value: string }[];
}

interface Score {
  /** 代码能跑通档得分（满分40） */
  runScore: number;
  /** 结果符合公式预期档得分（满分40） */
  formulaScore: number;
  /** 图表可读档得分（满分20） */
  chartScore: number;
  /** 三档之和 */
  total: number;
  /** 判分证据：理论值 vs 仿真值的具体数字对比 */
  evidence: string;
  /** 给规划员的返工意见，具体到错在哪一步；无问题则写"通过" */
  comments: string;
}

interface ProblemOutcome {
  /** 题号 */
  id: string;
  /** passed=过线；capped=轮数用尽仍未过线 */
  status: "passed" | "capped";
  /** 最后一轮采用的仿真途径 */
  approach: string;
  /** 实际使用的轮数 */
  rounds: number;
  /** 最后一轮总分 */
  total: number;
  /** 该题结果目录绝对路径 */
  outDir: string;
  /** .m 脚本路径；从未写出则为空串 */
  scriptPath: string;
  /** 一句话结论 */
  summary: string;
}

phase("建档并渲染课件");
const prep = await agent("统筹员").ask<{ outDir: string; pagesDir: string; pageCount: number; pdfTool: string; chunks: { from: number; to: number }[] }>(
  `完成四件事：\n` +
    `1) 用 Bash 在 ${outputBase} 下创建形如 run-<当前日期时间> 的子文件夹，返回其绝对路径作为 outDir（~ 开头的路径先解析为绝对 Windows 路径）；在其下再建 pages 子目录。\n` +
    `2) 定位 pdf-courseware skill 的 pdf_tools.py：${pdfToolArg ? `已由 pdfTool 参数指定：${pdfToolArg}` : "参数为空——从本会话 skill 列表找到 pdf-courseware 的 SKILL.md，pdf_tools.py 与其同目录，解析为绝对 Windows 路径"}。记为 pdfTool，放进返回值。\n` +
    `3) 渲染课件每页为 PNG：\n` +
    `   python "<pdfTool>" pages "${courseware}" "<pagesDir>" 150\n` +
    `   命令打印 "PAGES <n>"，记为 pageCount（首次运行会自动安装 pymupdf，属正常）。若命令失败，如实上报，不要用其他方式硬凑。\n` +
    `4) 按每段最多 12 页把 [1, pageCount] 切成若干段，1-based 闭区间，作为 chunks 返回。`,
);
const pdfTool = pdfToolArg || prep.pdfTool;
log(`课件共 ${prep.pageCount} 页，分 ${prep.chunks.length} 段读题，产物目录 ${prep.outDir}`);

phase("分片读题并汇总");
const candidateLists = await Promise.all(
  prep.chunks.map((c) =>
    agent(`读题员·第${c.from}-${c.to}页`).ask<Problem[]>(
      `你负责课件的第 ${c.from}-${c.to} 页（1-based）。课件：${courseware}\n` +
        `第一步：用文本扫描定位出题页：\n` +
        `   python "${pdfTool}" text "${courseware}" ${c.from} ${c.to}\n` +
        `   找出其中的例题/习题/需要数值仿真或绘图验证的任务。\n` +
        `第二步：对确认出题的页，用 Read 查看页面图像 ${prep.pagesDir}/page-<三位页码>.png（如 page-012.png），从图像抄录公式的准确形式——文本层的公式是乱码，一切以图像为准；图中文字看不清就单页重渲 200dpi 再看：\n` +
        `   python "${pdfTool}" page "${courseware}" <页码> "<临时.png>" 200\n` +
        `逐题返回：id（用课件里的题号）、title、description（含全部已知条件与要求）、formulas（从图像抄录）、sourcePage（如 "p.12" 或 "p.12-13"）。纯理论推导、无需仿真的内容不要收；本段没有题就返回空数组。不要编造题面条件。`,
    ),
  ),
);
// 整合需要全部读题员的产出，这里必须等齐
const problems = await agent("整合员").ask<Problem[]>(
  `下面是各段读题员交回的候选题目（JSON）。合并整理：同一道题被相邻段重复报或跨页拆散的，合并为一题（description 取并集，formulas 取更完整者，sourcePage 标页码范围）；按课件原题号排序；不得丢弃任何一道独立存在的题。返回最终题目数组。\n${JSON.stringify(candidateLists)}`,
);
log(`合并后共 ${problems.length} 道题`);

artifact.table("scores", {
  title: "各题评分进度",
  key: "id",
  columns: [
    { field: "id", label: "题号" },
    { field: "approach", label: "途径" },
    { field: "status", label: "状态" },
    { field: "total", label: "总分" },
    { field: "rounds", label: "轮数" },
  ],
});

const outcomes: ProblemOutcome[] = [];
for (let i = 0; i < problems.length; i += batchSize) {
  const batch = problems.slice(i, i + batchSize);
  log(`第 ${Math.floor(i / batchSize) + 1} 批：题 ${batch.map((p) => p.id).join("、")}`);
  const st = batch.map((p) => ({
    p,
    outDir: `${prep.outDir}/题${p.id}`,
    // 各角色 agent 在其所属阶段内惰性创建，使创建步骤与阶段一一对应
    planner: undefined as Agent | undefined,
    gatekeeper: undefined as Agent | undefined,
    executor: undefined as Agent | undefined,
    scorer: undefined as Agent | undefined,
    feedback: "首轮，无返工意见。",
    lastScript: "",
    lastApproach: "script",
    lastScore: 0,
    lastSummary: "",
    outcome: undefined as ProblemOutcome | undefined,
  }));

  for (let round = 1; round <= maxRounds; round++) {
    const active = st.filter((s) => !s.outcome);
    if (active.length === 0) break;
    log(`第 ${Math.floor(i / batchSize) + 1} 批第 ${round} 轮：题 ${active.map((s) => s.p.id).join("、")}`);

    phase("规划仿真计划");
    const plans = await Promise.all(
      active.map(async (s) => {
        s.planner = s.planner ?? agent(
          `规划员·题${s.p.id}`,
          "你制定 MATLAB 仿真计划：把题目要求拆成建模思路、公式推导、应输出的变量和图。仿真途径的选择规则：控制系统、动力学、框图类问题优先用 Simulink 建模（approach=simulink），只有很简单的问题（纯数值计算、简单绘图、没有系统结构）才直接写脚本（approach=script）。计划要可执行，不确定的假设写明；题目信息不足以仿真时如实说明，不要编造条件。",
        );
        const plan = await s.planner.ask<Plan>(
          `题目：${JSON.stringify(s.p)}\n返工意见：${s.feedback}\n请制定仿真计划（含 approach 的选择与理由）。`,
        );
        s.lastApproach = plan.approach;
        return { s, plan };
      }),
    );

    phase("编码并通过语法门禁");
    const gated = await Promise.all(
      plans.map(async ({ s, plan }) => {
        let code: CodeResult | undefined;
        let lint: ExecResult | undefined;
        for (let attempt = 1; attempt <= codeTries; attempt++) {
          const retry =
            attempt === 1
              ? ""
              : `上一版语法审查未通过，错误原样如下，必须修正后重写：lint=${JSON.stringify(lint?.lintErrors ?? [])}`;
          const simulinkAsk =
            `仿真计划：${JSON.stringify(plan)}\n用 Simulink 建模仿真：写一个自包含的 ${s.outDir}/sim.m——先用 new_system/add_block/add_line 按模型设计搭出模型并 save_system 存为 ${s.outDir}/model.slx，再加载模型运行仿真（sim 或 set_param），把关键结果存入工作区变量并绘图。开头为 clear; close all; clc;。${retry}`;
          const scriptAsk =
            `仿真计划：${JSON.stringify(plan)}\n编写 MATLAB 脚本，保存为 ${s.outDir}/sim.m，开头为 clear; close all; clc;。${retry}`;
          code = await s.coder.ask<CodeResult>(
            plan.approach === "simulink" ? simulinkAsk : scriptAsk,
          );
          s.lastScript = code.path;
          s.gatekeeper = s.gatekeeper ?? agent(
            `门禁员·题${s.p.id}`,
            "你是语法门禁：只调 mcp__matlab__lint_script 审查指定脚本，把报出的语法错误原样转述。不要运行、不要清场、不要导图。绝不把失败说成成功。",
          );
          lint = await s.gatekeeper.ask<ExecResult>(
            `只做语法门禁：mcp__matlab__lint_script 审查 ${code.path}，有语法错误就原样填入 lintErrors。不要运行、不要清场、不要导图，ran 固定填 false。`,
          );
          if (lint.lintErrors.length === 0) break;
        }
        return { s, plan, code: code!, lint: lint! };
      }),
    );

    phase("运行并收集结果");
    for (const g of gated.filter((g) => g.lint.lintErrors.length > 0)) {
      g.s.lastScore = 0;
      g.s.lastSummary = "代码未通过语法审查";
      report({ id: g.s.p.id, approach: g.s.lastApproach, status: "失败待返工", total: 0, rounds: round }, "scores");
      g.s.feedback = `第 ${round} 轮代码两次语法审查均未通过。lint=${JSON.stringify(g.lint.lintErrors)}。请重新规划，必要时简化实现方式。`;
    }
    const runs = await Promise.all(
      gated
        .filter((g) => g.lint.lintErrors.length === 0)
        .map(async ({ s, plan, code }) => {
          s.executor = s.executor ?? agent(
            `执行员·题${s.p.id}`,
            "你负责执行收集：清场、运行、导图、收集输出，一轮完成立即汇报。不要反复运行或自行调参调试——修改代码是编码员的事。所有工具返回值原样转述，绝不把失败说成成功。",
          );
          const exec = await s.executor.ask<ExecResult>(
            `处理脚本 ${code.path}（计划要收集的变量：${plan.expectedVariables}）：\n` +
              `1) 先 run_code("clear; close all; clc") 清场，再 run_script 运行（timeout_s 给 600）。运行报错则原样填入 consoleOutput，ran=false。\n` +
              `2) 运行成功：export_figure 导出全部图（只调一次），用 Bash cp 一条命令把 PNG 复制到 ${s.outDir}/，再 list_variables 一次 + get_variable 只取计划点名的变量（每个最多调用一次）。ran=true 仅当脚本完整跑完。consoleOutput 只留末尾 2000 字符。收齐后立即返回，不要做额外核对或重复取数。`,
          );
          return { s, exec };
        }),
    );
    for (const r of runs.filter((r) => !r.exec.ran)) {
      r.s.lastScore = 0;
      r.s.lastSummary = "运行失败";
      report({ id: r.s.p.id, approach: r.s.lastApproach, status: "失败待返工", total: 0, rounds: round }, "scores");
      r.s.feedback = `第 ${round} 轮运行失败。运行输出末尾=${r.exec.consoleOutput}。请重新规划，必要时简化模型。`;
    }

    phase("独立评分");
    const scored = await Promise.all(
      runs
        .filter((r) => r.exec.ran)
        .map(async ({ s, exec }) => {
          s.scorer = s.scorer ?? agent(
            `评分员·题${s.p.id}`,
            "你独立评分，没有参与规划和编码。理论值必须自己推导，仿真值必须自己用工具重新获取核对，不采信任何人的转述；发现不符就扣分并写明。控制成本：每张图最多查看一次，变量一次性取齐，不要重复读取相同文件或数据。",
          );
          const score = await s.scorer.ask<Score>(
            `独立为题${s.p.id}的仿真打分，满分 40+40+20。题目：${JSON.stringify(s.p)}\n` +
              `对题面有疑时可 Read 原页面图像 ${prep.pagesDir}/page-<三位页码>.png 核对（题面页码：${s.p.sourcePage}）。\n` +
              `脚本：${s.lastScript}（自己读代码）\n控制台输出末尾：${exec.consoleOutput}\n图：${exec.figures.join("、") || "无"}（逐张用 Read 查看）\n` +
              `三档标准：\n①代码能跑通(40)：以 lint 干净且完整运行为准；\n②结果符合公式预期(40)：自己从题面公式推导理论值，用 mcp__matlab__get_variable 重新取工作区变量核对仿真值，写出具体数字对比；\n③图表可读(20)：标题、轴标签、单位、图例齐全。\n` +
              `变量以你实际取到的为准；缺图、空图、变量与输出不符都要扣分并写明。`,
          );
          s.lastScore = score.total;
          s.lastSummary = score.comments;
          report({ id: s.p.id, approach: s.lastApproach, status: "已评分", total: score.total, rounds: round }, "scores");
          return { s, score };
        }),
    );
    for (const { s, score } of scored) {
      if (score.total >= threshold && score.formulaScore >= formulaFloor) {
        s.outcome = {
          id: s.p.id,
          status: "passed",
          approach: s.lastApproach,
          rounds: round,
          total: score.total,
          outDir: s.outDir,
          scriptPath: s.lastScript,
          summary: score.comments,
        };
      } else {
        s.feedback = `第 ${round} 轮总分 ${score.total} 未过线（过线需总分达标且公式预期档不低于下限）。评语：${score.comments}`;
      }
    }
  }
  for (const s of st) {
    if (s.outcome) {
      outcomes.push(s.outcome);
      continue;
    }
    report({ id: s.p.id, approach: s.lastApproach, status: "轮数用尽", total: s.lastScore, rounds: maxRounds }, "scores");
    outcomes.push({
      id: s.p.id,
      status: "capped",
      approach: s.lastApproach,
      rounds: maxRounds,
      total: s.lastScore,
      outDir: s.outDir,
      scriptPath: s.lastScript,
      summary: s.lastSummary || "多轮未过线",
    });
  }
}

const passedCount = outcomes.filter((o) => o.status === "passed").length;
const lines: string[] = [
  "# MATLAB 仿真评分报告",
  "",
  `课件：${courseware}`,
  `过线标准：总分 ≥ ${threshold} 且公式预期档 ≥ ${formulaFloor}`,
  `结果：${passedCount}/${outcomes.length} 题过线`,
  "",
];
for (const o of outcomes) {
  lines.push(`## 题${o.id} — ${o.status === "passed" ? "通过" : "未过线"}（第 ${o.rounds} 轮，${o.total} 分，途径：${o.approach}）`);
  lines.push("");
  lines.push(o.summary);
  lines.push("");
  lines.push(`脚本：${o.scriptPath || "无"}`);
  lines.push(`结果目录：${o.outDir}`);
  lines.push("");
}
await artifact.markdown("report", lines.join("\n"), {
  title: "MATLAB 仿真评分报告",
  description: `${passedCount}/${outcomes.length} 题过线，脚本与图在 ${prep.outDir}`,
  primary: true,
});

return {
  conclusion: `${outcomes.length} 道题中 ${passedCount} 道过线；.m 脚本、模型与图在 ${prep.outDir}。`,
  findings: outcomes
    .filter((o) => o.status === "capped")
    .map((o) => ({
      where: o.outDir,
      what: `题${o.id}（途径 ${o.approach}）在 ${o.rounds} 轮后仍未过线（${o.total} 分）：${o.summary}`,
      evidence: `脚本 ${o.scriptPath || "无"}；各轮评分证据见评分员记录`,
      status: "verified",
      severity: "medium",
    })),
  verified: [
    "拆题以页面图像为准（pdf-courseware skill 渲染），分片并行、整合员去重，页范围全覆盖",
    "每题脚本先经 lint_script 语法门禁（仅语法错误拦截）再运行",
    "脚本在 MATLAB 常驻会话中真实执行，控制台输出与图均已收集",
    "每条评分由独立评分员自行推导理论值并用 get_variable 重新核对仿真值",
  ],
  notCovered: [
    "整合员去重依赖其判断，理论上仍可能合并掉真正独立的两题",
    "代码风格与工程规范未审查，lint 的警告级（非语法错误）问题未强制处理",
    "公式预期档只有评分员一层独立推导，未做第二次推导",
  ],
};
