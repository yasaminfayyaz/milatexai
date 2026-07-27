// Engine test battery for tool_latexlint.html. Run in QuickJS after the engine
// (window.LATEXLINT defined). Returns JSON {pass, fail, total, failures}.
(function () {
  if (!window.LATEXLINT) return JSON.stringify({ pass: 0, fail: 1, total: 1, failures: [{ name: "window.LATEXLINT missing", detail: "" }] });
  var L = window.LATEXLINT.lint, R = [], pass = 0, fail = 0;
  function ok(name, cond, detail) { if (cond) pass++; else { fail++; R.push({ name: name, detail: String(detail === undefined ? "" : detail) }); } }
  function eq(name, a, b) { ok(name, a === b, "got=" + JSON.stringify(a) + " want=" + JSON.stringify(b)); }
  function res(s) { return L(s); }
  function nIss(s) { return L(s).issues.length; }
  function isOk(s) { return L(s).ok; }
  function atLine(s, ln) { return L(s).issues.some(function (x) { return x.line === ln; }); }
  function atCol(s, ln, cl) { return L(s).issues.some(function (x) { return x.line === ln && x.col === cl; }); }
  function msg(s, re) { return L(s).issues.some(function (x) { return re.test(x.msg); }); }
  function warnMsg(s, re) { return L(s).issues.some(function (x) { return x.severity === "warning" && re.test(x.msg); }); }
  function errMsg(s, re) { return L(s).issues.some(function (x) { return x.severity === "error" && re.test(x.msg); }); }
  // A valid-document assertion: no errors AND no warnings (the fail-proof guarantee).
  function clean(name, s) { var r = L(s); ok("clean." + name, r.ok, JSON.stringify(r.issues)); }

  // ============ balanced / trivial ============
  ok("ok.simple", isOk("\\documentclass{article}\n\\begin{document}\nHi.\n\\end{document}"));
  ok("ok.nestedBraces", isOk("{{{}}}"));
  ok("ok.newcommand", isOk("\\newcommand{\\x}{y}"));
  ok("ok.newcommandArg", isOk("\\newcommand{\\vec}[1]{\\mathbf{#1}}"));
  ok("ok.usepackageOpts", isOk("\\usepackage[utf8]{inputenc}\\documentclass[12pt]{article}"));
  ok("ok.nestedEnvs", isOk("\\begin{document}\\begin{itemize}\\item a\\end{itemize}\\end{document}"));
  ok("ok.emptyString", isOk(""));
  ok("ok.plainText", isOk("Just some ordinary prose with no special characters."));

  // ============ braces (line + column) ============
  var b1 = res("\\documentclass{article}\n\\begin{document}\nHello \\textbf{world.\n\\end{document}");
  eq("brace.missingClose.count", b1.issues.length, 1);
  ok("brace.missingClose.line3", atLine("\\documentclass{article}\n\\begin{document}\nHello \\textbf{world.\n\\end{document}", 3));
  ok("brace.missingClose.msg", msg("a\n\nHi \\textbf{x", /never closed/));
  eq("brace.extraClose.count", nIss("a}"), 1);
  ok("brace.extraClose.msg", msg("a}", /extra closing brace/));
  ok("brace.extraClose.col1", atCol("}", 1, 1));
  eq("brace.oneUnclosedOfThree", nIss("{{}"), 1);
  ok("brace.reportsOpenLine", atLine("line1\nline2 {\nline3", 2));
  ok("brace.column", atCol("  {", 1, 3));
  ok("brace.hasContext", (function () { var r = res("alpha {beta"); return r.issues.length === 1 && /alpha \{beta/.test(r.issues[0].context); })());

  // ============ comments and escapes ignored ============
  clean("commentBrace", "% this { is a comment\nok");
  clean("commentDollar", "% $ unclosed in comment\nok");
  clean("commentSpecials", "text % stray _ ^ & { in a comment\nmore text");
  clean("escapedBraces", "\\{ \\}");
  clean("escapedDollar", "\\$5 and \\$10 today");
  clean("escapedPercent", "50\\% done {x}");
  clean("escapedAmp", "Tom \\& Jerry");
  clean("escapedUnderscore", "file\\_name.txt");
  clean("escapedCaretAccent", "Se\\^{n}or");
  clean("doubleBackslash", "line one \\\\ line two");

  // ============ environments ============
  ok("env.unclosed.msg", msg("\\begin{itemize}\n\\item a", /itemize.*never closed/));
  ok("env.unclosed.line", atLine("x\n\\begin{itemize}\n\\item a", 2));
  ok("env.mismatch", msg("\\begin{itemize}\\item a\\end{enumerate}", /mismatched|itemize|enumerate/));
  ok("env.endNoBegin", msg("\\end{foo}", /no matching \\begin/));
  eq("env.balanced.ok", nIss("\\begin{a}\\begin{b}\\end{b}\\end{a}"), 0);
  ok("env.innerUnclosed", msg("\\begin{a}\\begin{b}\\end{a}", /never closed|begin\{b\}/));
  clean("env.sameNameNested", "\\begin{itemize}\\begin{itemize}\\item x\\end{itemize}\\end{itemize}");

  // ============ \end{document} stops scanning ============
  clean("doc.trailingJunkIgnored", "\\begin{document}\nhi\n\\end{document}\nleftover {unbalanced $ x_2 &");
  ok("doc.endNoBegin", msg("\\end{document}", /no matching \\begin\{document\}/));
  ok("doc.innerUnclosedBeforeEnd", msg("\\begin{document}\\begin{itemize}\\item a\\end{document}", /itemize.*never closed/));

  // ============ math delimiters ============
  clean("math.inline.ok", "$x^2 + y^2$");
  ok("math.inline.unclosed", errMsg("$x^2 + 1", /inline math \$.*never closed/));
  clean("math.display.ok", "\\[ x = 1 \\]");
  ok("math.displayBracket.unclosed", errMsg("\\[ x = 1", /display math \\\[.*never closed/));
  ok("math.rbrackNoOpen", errMsg("x \\]", /no matching \\\[/));
  clean("math.paren.ok", "\\( a+b \\)");
  ok("math.parenNoOpen", errMsg("a+b \\)", /no matching \\\(/));
  clean("math.dollarDollar.ok", "$$ E = mc^2 $$");
  ok("math.dollarDollar.unclosed", errMsg("$$ E = mc^2", /display math \$\$.*never closed/));
  ok("math.dollarDollar.singleClose", errMsg("$$ x $", /single \$/));
  clean("math.twoInline", "$a$ and $b$");
  clean("math.adjacentInline", "$a$$b$");
  clean("math.nestedTextInMath", "\\[ \\text{when $x>0$} \\]");
  clean("math.mathModeUnderscore", "$a_i^j$");

  // ============ verbatim / \verb / \lstinline ============
  clean("verb.envBracesIgnored", "\\begin{verbatim}\n{{{ $ \\foo unmatched _ ^ &\n\\end{verbatim}");
  clean("verb.lstlistingIgnored", "\\begin{lstlisting}\nif (x) { y = a_b & c }\n\\end{lstlisting}");
  clean("verb.mintedWithArg", "\\begin{minted}{python}\nd = {'a': 1\n\\end{minted}");
  clean("verb.inlineVerb", "code \\verb|{ unmatched _ & | more");
  clean("verb.verbStar", "code \\verb*|a b| more");
  clean("verb.lstinlinePipe", "code \\lstinline|x_y| more");
  clean("verb.lstinlineOpts", "code \\lstinline[language=C]|x = a_b| more");
  ok("verb.unclosed", errMsg("see \\verb|abc", /\\verb is not closed/));
  ok("verb.unclosedNewline", errMsg("\\verb|abc\ndef|", /not closed/));
  ok("verb.envUnclosed", errMsg("\\begin{verbatim}\ncode here", /verbatim.*never closed/));

  // ============ \left / \right ============
  clean("leftright.ok", "$\\left( x \\right)$");
  ok("leftright.missingRight", errMsg("$\\left( x $", /\\left.*no matching \\right/));
  ok("leftright.extraRight", errMsg("$ x \\right) $", /\\right has no matching \\left/));
  clean("leftright.escapedBraces", "$\\left\\{ x \\right\\}$");
  clean("leftright.dot", "$\\left. \\frac{a}{b} \\right|_{x=0}$");
  clean("leftright.notLeftarrow", "$a \\leftarrow b \\rightarrow c$");

  // ============ advisory: _ ^ & in text (warnings), fail-proof ============
  ok("warn.underscoreText", warnMsg("the value x_2 is", /Missing \$ inserted|text mode/));
  ok("warn.underscoreText.isWarning", res("x_2").warnings === 1 && res("x_2").errors === 0);
  ok("warn.caretText", warnMsg("E = mc^2 in text", /text mode|Missing \$/));
  ok("warn.ampText", warnMsg("Tom & Jerry", /alignment character &/));
  // safe negatives: these valid patterns must NOT warn
  clean("safe.underscoreInMath", "$x_2$ and \\[ y_3 \\]");
  clean("safe.caretInMath", "$x^2$");
  clean("safe.underscoreInLabel", "\\label{fig:my_fig_2}");
  clean("safe.underscoreInCite", "As shown by \\cite{smith_etal_2020}.");
  clean("safe.underscoreInRef", "See \\ref{sec_intro} and \\eqref{eq_main}.");
  clean("safe.underscoreInUrl", "\\url{https://example.com/a_b?x=1&y=2}");
  clean("safe.underscoreInHref", "\\href{http://a_b.com}{link}");
  clean("safe.underscoreInGraphics", "\\includegraphics[width=2cm]{plot_final_1}");
  clean("safe.underscoreInInput", "\\input{sections/intro_part}");
  clean("safe.underscoreInBib", "\\bibliography{refs_main_2020}");
  clean("safe.ampInTabular", "\\begin{tabular}{cc} a & b \\\\ c & d \\end{tabular}");
  clean("safe.ampInAlign", "\\begin{align} E &= mc^2 \\\\ a_i &= b_i \\end{align}");
  clean("safe.ampInCases", "\\[ f(x)=\\begin{cases} 1 & x>0 \\\\ 0 & x<0 \\end{cases} \\]");
  clean("safe.underscoreInAlignEnv", "\\begin{equation} x_1 + x_2 = y^2 \\end{equation}");
  clean("safe.ampInMatrix", "$\\begin{matrix} a & b \\\\ c & d \\end{matrix}$");

  // ============ multiple issues, sorting, counts ============
  var multi = res("\\begin{document}\n\\textbf{bold\n$math\n\\begin{itemize}\n\\item a\n\\end{document}");
  ok("multi.several", multi.issues.length >= 2, JSON.stringify(multi.issues));
  ok("multi.sorted", (function () { for (var i = 1; i < multi.issues.length; i++) { if (multi.issues[i].line < multi.issues[i - 1].line) return false; } return true; })());
  ok("multi.errWarnCounts", (function () { var r = res("x_2 and \\textbf{y"); return r.warnings === 1 && r.errors === 1; })());

  // ============ result shape ============
  var shape = res("a}");
  ok("shape.hasOk", typeof shape.ok === "boolean");
  ok("shape.counts", typeof shape.errors === "number" && typeof shape.warnings === "number");
  ok("shape.issueFields", shape.issues.length > 0 && typeof shape.issues[0].line === "number" && typeof shape.issues[0].col === "number" && typeof shape.issues[0].msg === "string" && typeof shape.issues[0].severity === "string");

  // ============ realistic valid documents: zero false positives ============
  clean("real.article", [
    "\\documentclass[11pt]{article}",
    "\\usepackage[utf8]{inputenc}",
    "\\usepackage{amsmath,graphicx,booktabs,hyperref}",
    "\\newcommand{\\R}{\\mathbb{R}}",
    "\\title{A Study of DNA-seq at 50\\% Coverage}",
    "\\begin{document}",
    "\\maketitle",
    "\\section{Introduction}",
    "As shown in~\\cite{smith2020}, we have $x^2 + y^2 = z^2$. %comment {",
    "\\[ \\int_0^1 f(x)\\,dx = \\left( \\frac{a}{b} \\right). \\]",
    "\\begin{align} a &= b + c \\\\ d &= e \\end{align}",
    "\\begin{itemize}",
    "  \\item First \\& foremost, cost is \\$5.",
    "  \\item Use \\verb|{unbalanced| inline.",
    "\\end{itemize}",
    "\\begin{table}[htbp]\\centering",
    "  \\begin{tabular}{|l|c|r|}\\toprule A & B & C \\\\ \\bottomrule \\end{tabular}",
    "\\end{table}",
    "\\begin{lstlisting}",
    "def f(x): return { 'a': 1 & 2",
    "\\end{lstlisting}",
    "See \\url{https://example.com/a_b?p=1&q=2} and Fig.~\\ref{fig:x_1}.",
    "\\end{document}",
    "trailing { junk with $ and _ ignored"
  ].join("\n"));

  clean("real.mathHeavy", [
    "\\begin{document}",
    "\\begin{equation} \\label{eq:main}",
    "  \\mathcal{L} = \\sum_{i=1}^{n} \\left\\| x_i - \\hat{x}_i \\right\\|^2",
    "\\end{equation}",
    "The loss in Eq.~\\eqref{eq:main} depends on $\\theta_j^{(t)}$.",
    "\\begin{cases} a & b \\\\ c & d \\end{cases}",
    "\\end{document}"
  ].join("\n"));

  clean("real.tikz", [
    "\\begin{document}",
    "\\begin{tikzpicture}",
    "  \\draw (0,0) -- (1,1);",
    "  \\node at (0.5,0.5) {$x_1$};",
    "\\end{tikzpicture}",
    "\\end{document}"
  ].join("\n"));

  clean("real.tabularWithMathCells", "\\begin{tabular}{cc} $a_1$ & $b^2$ \\\\ $c_3$ & $d^4$ \\end{tabular}");

  // ============ more real-world valid snippets (regression-guarded, must stay clean) ============
  clean("wild.siunitx", "\\SI{5}{\\meter\\per\\second} and \\si{\\kilogram}");
  clean("wild.multicolumn", "\\begin{tabular}{ccc}\\multicolumn{2}{c}{Header} & X \\\\ a & b & c\\end{tabular}");
  clean("wild.cline", "\\begin{tabular}{ccc} a & b & c \\\\ \\cline{2-3} d & e & f \\end{tabular}");
  clean("wild.mhchem", "\\ce{H2SO4} and \\ce{SO4^{2-}} here");
  clean("wild.hrefDoi", "\\href{https://doi.org/10.1000/xyz_123}{DOI link}");
  clean("wild.nestedFrac", "\\[ \\frac{\\partial f}{\\partial x_i} = \\sum_{j} a_{ij} \\]");
  clean("wild.arrayEnv", "\\[ \\begin{array}{cc} a & b \\\\ c & d \\end{array} \\]");
  clean("wild.verbatimSpecials", "\\begin{verbatim}\na_b & c % not $ math\n\\end{verbatim}");
  clean("wild.verbAmp", "The macro \\verb=\\&= prints an ampersand.");
  clean("wild.nestedTabular", "\\begin{tabular}{c}\\begin{tabular}{cc} a & b \\end{tabular}\\end{tabular}");
  clean("wild.accents", "\\^{o} and \\~{n} and \\'{e} are accents.");
  clean("wild.subsupText", "H\\textsubscript{2}O and 4\\textsuperscript{th}");
  clean("wild.enumOpt", "\\begin{enumerate}[label=(\\alph*)]\\item x\\end{enumerate}");
  clean("wild.bracesInMath", "$\\{a, b\\}$ and $\\left\\{ x \\right\\}$");
  clean("wild.footnoteUrl", "\\footnote{See \\url{a_b.com} for x\\_y}.");
  clean("wild.currencyEscaped", "It costs \\$5 to \\$10 per unit.");

  return JSON.stringify({ pass: pass, fail: fail, total: pass + fail, failures: R });
})();
