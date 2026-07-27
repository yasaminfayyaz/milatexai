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
  function msg(s, re) { return L(s).issues.some(function (x) { return re.test(x.msg); }); }

  // ---- balanced documents: no issues ----
  ok("ok.simple", isOk("\\documentclass{article}\n\\begin{document}\nHi.\n\\end{document}"));
  ok("ok.nestedBraces", isOk("{{{}}}"));
  ok("ok.newcommand", isOk("\\newcommand{\\x}{y}"));
  ok("ok.usepackageOpts", isOk("\\usepackage[utf8]{inputenc}\\documentclass[12pt]{article}"));
  ok("ok.nestedEnvs", isOk("\\begin{document}\\begin{itemize}\\item a\\end{itemize}\\end{document}"));
  ok("ok.emptyString", isOk(""));

  // ---- unmatched braces ----
  var r1 = res("\\documentclass{article}\n\\begin{document}\nHello \\textbf{world.\n\\end{document}");
  eq("brace.missingClose.count", r1.issues.length, 1);
  ok("brace.missingClose.line3", atLine("\\documentclass{article}\n\\begin{document}\nHello \\textbf{world.\n\\end{document}", 3), JSON.stringify(r1.issues));
  ok("brace.missingClose.msg", msg("a\n\nHi \\textbf{x", /never closed/));
  eq("brace.extraClose.count", nIss("a}"), 1);
  ok("brace.extraClose.msg", msg("a}", /extra closing brace/));
  eq("brace.oneUnclosedOfThree", nIss("{{}"), 1);
  ok("brace.reportsOpenLine", atLine("line1\nline2 {\nline3", 2));

  // ---- comments and escapes are ignored ----
  ok("ignore.commentBrace", isOk("% this { is a comment\nok"));
  ok("ignore.commentDollar", isOk("% $ unclosed in comment\nok"));
  ok("ignore.escapedBraces", isOk("\\{ \\}"));
  ok("ignore.escapedDollar", isOk("\\$5 and \\$10 today"));
  ok("ignore.escapedPercent", isOk("50\\% done {x}"));
  ok("ignore.escapedPercentNoSwallow", isOk("a \\% b {c} d"));

  // ---- environments ----
  ok("env.unclosed.msg", msg("\\begin{itemize}\n\\item a", /itemize.*never closed/));
  ok("env.unclosed.line", atLine("x\n\\begin{itemize}\n\\item a", 2));
  ok("env.mismatch", msg("\\begin{itemize}\\item a\\end{enumerate}", /itemize|enumerate/));
  ok("env.endNoBegin", msg("\\end{foo}", /no matching \\begin/));
  eq("env.balanced.ok", nIss("\\begin{a}\\begin{b}\\end{b}\\end{a}"), 0);
  ok("env.innerUnclosed", msg("\\begin{a}\\begin{b}\\end{a}", /b.*never closed|begin\{b\}/));

  // ---- \end{document} stops scanning (trailing junk ignored) ----
  ok("doc.trailingJunkIgnored", isOk("\\begin{document}\nhi\n\\end{document}\nleftover {unbalanced $"));
  ok("doc.endNoBegin", msg("\\end{document}", /no matching \\begin\{document\}/));

  // ---- math delimiters ----
  ok("math.inline.ok", isOk("$x^2 + y^2$"));
  ok("math.inline.unclosed", msg("$x^2 + 1", /inline math \$.*never closed/));
  ok("math.display.ok", isOk("\\[ x = 1 \\]"));
  ok("math.displayBracket.unclosed", msg("\\[ x = 1", /display math \\\[.*never closed/));
  ok("math.rbrackNoOpen", msg("x \\]", /no matching \\\[/));
  ok("math.paren.ok", isOk("\\( a+b \\)"));
  ok("math.dollarDollar.ok", isOk("$$ E = mc^2 $$"));
  ok("math.dollarDollar.unclosed", msg("$$ E = mc^2", /display math \$\$.*never closed/));

  // ---- verbatim-like blocks are skipped ----
  ok("verb.envBracesIgnored", isOk("\\begin{verbatim}\n{{{ $ \\foo unmatched\n\\end{verbatim}"));
  ok("verb.lstlistingIgnored", isOk("\\begin{lstlisting}\nif (x) { y }\n\\end{lstlisting}"));
  ok("verb.inlineVerb", isOk("code \\verb|{ unmatched| more"));
  ok("verb.mintedWithArg", isOk("\\begin{minted}{python}\nd = {'a': 1\n\\end{minted}"));

  // ---- \left \right ----
  ok("leftright.ok", isOk("$\\left( x \\right)$"));
  ok("leftright.missingRight", msg("$\\left( x $", /\\left.*no matching \\right/));
  ok("leftright.extraRight", msg("$ x \\right) $", /\\right has no matching \\left/));
  ok("leftright.escapedBraces", isOk("$\\left\\{ x \\right\\}$"));

  // ---- multiple errors, sorted by line ----
  var multi = res("\\begin{document}\n\\textbf{bold\n$math\n\\begin{itemize}\n\\item a\n\\end{document}");
  ok("multi.several", multi.issues.length >= 2, JSON.stringify(multi.issues));
  ok("multi.sorted", (function () { for (var i = 1; i < multi.issues.length; i++) { if (multi.issues[i].line < multi.issues[i - 1].line) return false; } return true; })());

  // ---- result shape ----
  var shape = res("a}");
  ok("shape.hasOk", typeof shape.ok === "boolean");
  ok("shape.issueFields", shape.issues.length > 0 && typeof shape.issues[0].line === "number" && typeof shape.issues[0].msg === "string");

  return JSON.stringify({ pass: pass, fail: fail, total: pass + fail, failures: R });
})();
