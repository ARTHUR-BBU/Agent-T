# -*- coding: utf-8 -*-
"""B7-6 C1：index.html 三屏结构重组为 Gemini 式整壳（阳仔线框 §1/§3）。

程序化搬运（禁手拼——B7-5b 教训）：按标记提取原节点块，逐字嵌入新壳，
不转写任何既有节点内容。
"""
import io

P = "app/static/index.html"
lines = io.open(P, encoding="utf-8").readlines()


def idx(pred, start=0):
    for i in range(start, len(lines)):
        if pred(lines[i]):
            return i
    raise SystemExit(f"marker not found after {start}")


# ---- 提取块 ----
# 上传表单卡（screen-upload 内的设置卡）：从「合同类型」label 前的 card 开始
i_form_start = idx(lambda l: l.strip() == "<div class=\"card\">",
                   idx(lambda l: "合同类型" in l) - 6)
i_form_end = idx(lambda l: l.strip() == "</div>",
                 idx(lambda l: "privacy-note" in l)) + 1
UP_FORM = lines[i_form_start:i_form_end]

# 进度卡
i_prog = idx(lambda l: 'id="results-loading"' in l)
i_err = idx(lambda l: 'id="results-error"' in l)
PROGRESS = lines[i_prog:i_err]
RESULTS_ERR = lines[i_err:i_err + 1]

# results-body 整块
i_rb = idx(lambda l: 'id="results-body"' in l)
i_cols_close = idx(lambda l: "/results-cols -->" in l, i_rb)
i_rb_end = i_cols_close + 1  # 该行之后是 results-body 的收口 </div>
assert lines[i_rb_end].strip() == "</div>", lines[i_rb_end]
RBODY = lines[i_rb:i_rb_end + 1]

# ask 三节点块（note-card 起，至 ask-answer 所在卡收口）
i_ask = idx(lambda l: 'id="ask-note-card"' in l)
i_ans = idx(lambda l: 'id="ask-answer"' in l)
i_ask_end = idx(lambda l: l.strip() == "</div>", i_ans) + 1
ASK_NODES = lines[i_ask:i_ask_end]

# precheck dialog
i_pc = idx(lambda l: 'id="precheck-dialog"' in l) - 2  # 含前导注释
i_pc_end = idx(lambda l: l.strip() == "</dialog>", i_pc) + 1
PRECHECK = lines[i_pc:i_pc_end]

# ---- 重组 ----
head_end = idx(lambda l: l.strip() == "<body>")
head = lines[:head_end + 1]

ASK_WORKBENCH = [
    "    <!-- B7-6 对话工作台（线框 §3.3）：桌面=col-side 详情之下；<1024=\n",
    "         col-main 三卡之后（matchMedia 搬迁清单扩为 detail+workbench）。\n",
    "         原 #screen-ask 节点平移至此；四段式/chips/seq 判废全保留 -->\n",
    "    <section id=\"ask-workbench\" class=\"ask-workbench hidden\" aria-label=\"对话工作台\">\n",
    "      <p id=\"ask-title\" class=\"hidden\"></p>\n",
    "      <p class=\"ask-status-line\" id=\"ask-status-line\"></p>\n",
] + ASK_NODES + [
    "    </section>\n",
]

new = []
new.extend(head)
new.append("  <header id=\"shell-top\">\n")
new.append("    <button type=\"button\" id=\"shell-menu\" class=\"shell-menu\" aria-label=\"打开导航菜单\" aria-expanded=\"false\">☰</button>\n")
new.append("    <span class=\"shell-logo\">合同审查</span>\n")
new.append("  </header>\n")
new.append("  <div id=\"drawer-mask\" class=\"hidden\"></div>\n")
new.append("  <div id=\"shell\">\n")
new.append("    <nav id=\"shell-nav\">\n")
new.append("      <button id=\"btn-new-review\" class=\"primary shell-new\" type=\"button\">＋ 新审查</button>\n")
new.append("      <p class=\"shell-sect\">历史审查</p>\n")
new.append("      <ul id=\"shell-history\" class=\"shell-history\"></ul>\n")
new.append("      <p class=\"shell-sect hidden\" id=\"shell-context-sect\">本份合同</p>\n")
new.append("      <ul id=\"shell-context\" class=\"shell-context hidden\"></ul>\n")
new.append("    </nav>\n")
new.append("    <div id=\"shell-main\" data-ws=\"empty\">\n")
new.append("      <div class=\"ws-inner\">\n")
new.append("        <!-- ws-empty：上传融入右工作区（阳仔线框 §3.1；原 screen-upload\n")
new.append("             内容逐字平移，品类/立场/铁律旁注/HTTP 警示/隐私说明一个不丢） -->\n")
new.append("        <div id=\"ws-empty\">\n")
new.append("          <div class=\"card ws-guide\">\n")
new.append("            <p class=\"ws-guide-title\">把合同交给我，我来逐条核对。</p>\n")
new.append("            <p class=\"muted\">PDF、Word 都行，也可以直接粘贴文本。（占位文案过九哥）</p>\n")
new.append("          </div>\n")
new.append("          <div class=\"ws-entry-cards\">\n")
new.append("            <button type=\"button\" class=\"ws-entry\" id=\"entry-file\"><span class=\"ws-entry-main\">📄 上传合同文件</span><span class=\"ws-entry-sub\">点选或拖入文件</span></button>\n")
new.append("            <button type=\"button\" class=\"ws-entry\" id=\"entry-paste\"><span class=\"ws-entry-main\">📋 粘贴合同文本</span><span class=\"ws-entry-sub\">复制正文直接开始</span></button>\n")
new.append("          </div>\n")
new.append("          <div id=\"ws-settings\" class=\"hidden\">\n")
new.append("            <!-- B7-6 附录 B：粘贴入口（互斥切换；超限禁用提交不截短） -->\n")
new.append("            <div id=\"paste-wrap\" class=\"card hidden\">\n")
new.append("              <label class=\"field\"><span>粘贴合同正文</span>\n")
new.append("                <textarea id=\"paste-text\" rows=\"10\" placeholder=\"把合同正文粘贴到这里\"></textarea>\n")
new.append("              </label>\n")
new.append("              <p class=\"muted\" id=\"paste-count\"></p>\n")
new.append("              <p id=\"paste-error\" class=\"error hidden\"></p>\n")
new.append("            </div>\n")
new.append("            <div id=\"file-wrap\" class=\"hidden\">\n")
new.append("              <label class=\"field file-field\">\n")
new.append("                <span>选择文件</span>\n")
new.append("                <input type=\"file\" id=\"file\" accept=\".txt,.md,.pdf,.docx,.doc,text/plain\" />\n")
new.append("              </label>\n")
new.append("            </div>\n")
new.extend(UP_FORM)
new.append("          </div>\n")
new.append("        </div><!-- /ws-empty -->\n")
new.append("        <!-- ws-running：真实进度三段清单原样平移 -->\n")
new.append("        <div id=\"ws-running\" class=\"hidden\">\n")
new.extend(PROGRESS)
new.extend(RESULTS_ERR)
new.append("        </div>\n")
new.append("        <!-- ws-results：B7-5b 双栏内容原样搬进新壳（ID 全保留） -->\n")
new.append("        <div id=\"ws-results\" class=\"hidden\">\n")
new.append("          <header class=\"top\">\n")
new.append("            <button type=\"button\" class=\"link\" id=\"btn-back-upload\">← 重新上传</button>\n")
new.append("            <h1>审查结果</h1>\n")
new.append("          </header>\n")
new.extend(RBODY)
new.append("          ")
new.extend(ASK_WORKBENCH)
new.append("        </div><!-- /ws-results -->\n")
new.append("      </div><!-- /ws-inner -->\n")
new.append("    </div><!-- /shell-main -->\n")
new.append("  </div><!-- /shell -->\n")
new.extend(PRECHECK)
new.append("  <script src=\"/static/app.js\"></script>\n")
new.append("</body>\n")
new.append("</html>\n")

io.open(P, "w", encoding="utf-8", newline="").writelines(new)
print("rebuilt:", len(lines), "->", len(new), "lines")
