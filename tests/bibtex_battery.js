// Engine test battery for tool_bibtex.html. Run in QuickJS AFTER the engine
// (window.BIB defined). Returns a JSON string {pass, fail, total, failures}.
(function () {
  if (!window.BIB) return JSON.stringify({ pass: 0, fail: 1, total: 1, failures: [{ name: "window.BIB missing", detail: "" }] });
  var B = window.BIB, R = [], pass = 0, fail = 0;
  function ok(name, cond, detail) { if (cond) pass++; else { fail++; R.push({ name: name, detail: String(detail === undefined ? "" : detail) }); } }
  function eq(name, a, b) { ok(name, a === b, "got=" + JSON.stringify(a) + " want=" + JSON.stringify(b)); }
  var p, c, d, e, iss;

  // ---- parsing ----
  p = B.parseBib("@article{k1,\n title={Hello},\n year={2020}\n}");
  eq("parse.count", p.entries.length, 1);
  eq("parse.type", p.entries[0].type, "article");
  eq("parse.key", p.entries[0].key, "k1");
  eq("parse.title", B.field(p.entries[0], "title"), "Hello");
  eq("parse.year", B.field(p.entries[0], "year"), "2020");

  p = B.parseBib('@article{k2, title = "A {Nested} title", author = "X"}');
  eq("parse.quoted+nested", B.field(p.entries[0], "title"), "A {Nested} title");
  eq("parse.quoted.author", B.field(p.entries[0], "author"), "X");

  p = B.parseBib("@article{k3, year = 2021 }");
  eq("parse.bare", B.field(p.entries[0], "year"), "2021");

  p = B.parseBib('@string{pub = "ACM"}\n@comment{ignore me}\n@article{k4, title={T}}');
  eq("parse.meta+entry", p.entries.length, 2);
  ok("parse.string.isMeta", p.entries[0].meta === true);
  eq("parse.comment.skipped.type", p.entries[1].type, "article");

  // messy: leading-comma field, extra whitespace, duplicate field kept-first
  p = B.parseBib("@article{k5,\n  title = {A}\n  ,author={B}  , author={C}\n}");
  eq("parse.messy.title", B.field(p.entries[0], "title"), "A");
  eq("parse.dupfield.first", B.field(p.entries[0], "author"), "B");

  p = B.parseBib("@book{a, title={A}}\n@article{b, title={B}}");
  eq("parse.multi", p.entries.length, 2);

  p = B.parseBib('@article{k7, title = "Foo" # " Bar"}');
  eq("parse.concat", B.field(p.entries[0], "title"), "Foo Bar");

  // paren-delimited entry
  p = B.parseBib("@article(k8, title={Paren})");
  eq("parse.paren", B.field(p.entries[0], "title"), "Paren");

  // equals sign inside a braced value must not break field parsing
  p = B.parseBib("@misc{k9, url={http://x.com/a=b&c=d}, title={T}}");
  eq("parse.equals-in-value.url", B.field(p.entries[0], "url"), "http://x.com/a=b&c=d");
  eq("parse.equals-in-value.title", B.field(p.entries[0], "title"), "T");

  // empty input
  eq("parse.empty", B.parseBib("").entries.length, 0);
  eq("parse.junk", B.parseBib("not a bib file at all").entries.length, 0);

  // ---- normDOI ----
  eq("normDOI.prefix", B.normDOI("https://doi.org/10.1/AbC"), "10.1/abc");
  eq("normDOI.doicolon", B.normDOI("doi:10.2/x"), "10.2/x");
  eq("normDOI.empty", B.normDOI(""), "");

  // ---- protectTitle ----
  eq("protect.DNA", B.protectTitle("The DNA study"), "The {DNA} study");
  eq("protect.NASA", B.protectTitle("NASA report"), "{NASA} report");
  eq("protect.LaTeX", B.protectTitle("A LaTeX guide"), "A {LaTeX} guide");
  eq("protect.normal", B.protectTitle("A simple title"), "A simple title");
  eq("protect.alreadyBraced", B.protectTitle("A {DNA} title"), "A {DNA} title");
  eq("protect.lowercase", B.protectTitle("dna sequencing"), "dna sequencing");
  eq("protect.allCapsTitle.leftAlone", B.protectTitle("A STUDY OF DNA"), "A STUDY OF DNA");

  // ---- titleCaseIfShouting ----
  eq("shout.fix", B.titleCaseIfShouting("A STUDY OF THINGS"), "A Study Of Things");
  eq("shout.noop", B.titleCaseIfShouting("A normal title"), "A normal title");
  eq("shout.short.noop", B.titleCaseIfShouting("DNA"), "DNA");

  // ---- dedupe ----
  d = B.dedupe(B.parseBib("@article{x, title={A}, year={2020}}\n@article{x, journal={N}}").entries);
  eq("dedupe.byKey.merged", d.merged, 1);
  eq("dedupe.byKey.kept", d.entries.length, 1);
  eq("dedupe.byKey.fill", B.field(d.entries[0], "journal"), "N");
  eq("dedupe.byKey.keepFirst.year", B.field(d.entries[0], "year"), "2020");

  d = B.dedupe(B.parseBib("@article{a, doi={10.1/x}, title={A}}\n@article{b, doi={10.1/X}}").entries);
  eq("dedupe.byDOI.caseinsensitive", d.merged, 1);

  d = B.dedupe(B.parseBib("@article{a, title={A quite specific long title about frogs}}\n@article{b, title={A quite specific long title about frogs}}").entries);
  eq("dedupe.byTitle", d.merged, 1);

  d = B.dedupe(B.parseBib("@article{a, title={Alpha}}\n@article{b, title={Beta}}").entries);
  eq("dedupe.noFalseMerge", d.merged, 0);

  // ---- fuzzy possible-duplicate flagging (flag for review, never auto-merge) ----
  // different keys, one title has a subtitle, same author+year -> flagged, both kept
  c = B.clean("@article{a1, title={Deep learning for vision}, author={Smith, J}, journal={J}, year={2020}}\n@article{a2, title={Deep learning for vision: a comprehensive review}, author={Smith, J}, journal={J}, year={2020}}", { dedupe: true, sort: false, strip: true, brace: false, lower: true });
  eq("fuzzy.keepsBoth", c.stats.nEntries, 2);
  ok("fuzzy.flags", c.stats.possibleDupes >= 1 && c.stats.issues.some(function (x) { return /possible duplicate/.test(x.msg); }), JSON.stringify(c.stats.issues));
  // genuinely different references -> not flagged
  c = B.clean("@article{x, title={Quantum error correction codes}, author={Alice, A}, year={2019}}\n@article{y, title={A history of medieval poetry}, author={Bob, B}, year={2001}}", { dedupe: true, sort: false, strip: true, brace: false, lower: true });
  eq("fuzzy.noFalsePositive", c.stats.possibleDupes, 0);
  // same title but conflicting DOIs -> kept separate, not merged, not flagged
  c = B.clean("@article{p, title={Deep learning study today}, doi={10.1/aaa}}\n@article{q, title={Deep learning study today}, doi={10.1/bbb}}", { dedupe: true, sort: false, strip: true, brace: false, lower: true });
  eq("fuzzy.conflictDOI.keepsBoth", c.stats.nEntries, 2);
  eq("fuzzy.conflictDOI.notMerged", c.stats.merged, 0);
  eq("fuzzy.conflictDOI.notFlagged", c.stats.possibleDupes, 0);
  // firstAuthorLast parsing (both bibtex author styles)
  eq("firstAuthorLast.comma", B.firstAuthorLast("Smith, Jane and Doe, John"), "smith");
  eq("firstAuthorLast.natural", B.firstAuthorLast("Jane Smith and John Doe"), "smith");

  // ---- validate ----
  e = B.parseBib("@article{v, title={T}, year={2020}}").entries[0];
  iss = B.validate(e);
  ok("validate.article.missing", iss.some(function (x) { return /journal/.test(x.msg); }) && iss.some(function (x) { return /author/.test(x.msg); }), JSON.stringify(iss));
  e = B.parseBib("@book{b, title={T}, publisher={P}, year={2020}}").entries[0];
  ok("validate.book.needsAuthorOrEditor", B.validate(e).some(function (x) { return /author.*editor|editor.*author/.test(x.msg); }), JSON.stringify(B.validate(e)));
  e = B.parseBib("@book{b2, title={T}, publisher={P}, year={2020}, editor={E}}").entries[0];
  ok("validate.book.editorSatisfies", !B.validate(e).some(function (x) { return /author/.test(x.msg); }));
  e = B.parseBib("@misc{m, note={anything}}").entries[0];
  eq("validate.misc.noReq", B.validate(e).length, 0);

  // ---- clean pipeline / format ----
  c = B.clean("@article{smith,\ntitle={A study of DNA and RNA},\nauthor={Smith, J},\njournal={Nature},\nyear={2021}\n}", { dedupe: true, sort: true, strip: true, brace: true, lower: true });
  ok("clean.hasEntry", c.text.indexOf("@article{smith") >= 0, c.text);
  ok("clean.protectsAcronyms", /\{DNA\}/.test(c.text) && /\{RNA\}/.test(c.text), c.text);
  eq("clean.nEntries", c.stats.nEntries, 1);
  c = B.clean("@article{u, TITLE={T}, AUTHOR={A}, journal={J}, year={2020}}", { dedupe: false, sort: false, strip: true, brace: false, lower: true });
  ok("clean.lowercasesFieldNames", /\btitle\s*=/.test(c.text) && c.text.indexOf("TITLE") < 0 && c.text.indexOf("AUTHOR") < 0, c.text);
  // ALL-CAPS titles are flagged, not silently transformed (that would destroy acronyms)
  c = B.clean("@article{ac, title={A STUDY OF THINGS}, author={A}, journal={J}, year={2020}}", { dedupe: false, sort: false, strip: true, brace: true, lower: true });
  ok("clean.allcaps.warns", c.stats.issues.some(function (x) { return /ALL-CAPS/.test(x.msg); }), JSON.stringify(c.stats.issues));
  ok("clean.allcaps.notBraced", c.text.indexOf("{STUDY}") < 0 && c.text.indexOf("{THINGS}") < 0, c.text);

  c = B.clean("@article{m, title={T}, author={A}, journal={J}, year={2020}, month={January}}", { dedupe: false, sort: false, strip: true, brace: false, lower: true });
  ok("clean.month->jan", /month\s*=\s*jan/.test(c.text), c.text);
  ok("clean.month.notBraced", c.text.indexOf("{jan}") < 0, c.text);

  c = B.clean("@article{s, title={T}, author={A}, journal={J}, year={2020}, note={}}", { dedupe: false, sort: false, strip: true, brace: false, lower: true });
  ok("clean.stripsEmpty", c.text.indexOf("note") < 0, c.text);

  // sort by key
  c = B.clean("@article{zeta, title={Z}}\n@article{alpha, title={A}}", { dedupe: false, sort: true, strip: true, brace: false, lower: true });
  ok("clean.sorted", c.text.indexOf("alpha") < c.text.indexOf("zeta"), c.text);

  // no trailing comma on the last field
  c = B.clean("@article{t, author={A}, title={T}}", { dedupe: false, sort: false, strip: true, brace: false, lower: true });
  ok("clean.noTrailingComma", /title\s*=\s*\{T\}\s*\n\}/.test(c.text), c.text);

  // meta passthrough survives a clean
  c = B.clean('@string{acm = "ACM"}\n@article{x, title={T}, author={A}, journal={acm}, year={2020}}', { dedupe: true, sort: true, strip: true, brace: false, lower: true });
  ok("clean.keepsString", c.text.indexOf("@string{") >= 0, c.text);

  // ---- round trip ----
  var orig = "@article{rt,\n  author = {Doe, J},\n  title  = {A Title},\n  year   = {2019}\n}";
  var re = B.parseBib(B.clean(orig, { dedupe: false, sort: false, strip: true, brace: false, lower: true }).text).entries[0];
  eq("roundtrip.author", B.field(re, "author"), "Doe, J");
  eq("roundtrip.title", B.field(re, "title"), "A Title");
  eq("roundtrip.year", B.field(re, "year"), "2019");

  // ---- DOI finder scoring (pickBestDoi): robust, must never return a wrong DOI ----
  var de = B.parseBib("@article{k, title={Deep learning for image recognition}, author={Smith, Jane}, year={2020}}").entries[0];
  // near-identical title -> accept
  eq("doi.strongTitle", B.pickBestDoi(de, [{ DOI: "10.1/aaa", title: ["Deep learning for image recognition"], author: [{ family: "Smith" }], issued: { "date-parts": [[2020]] } }]), "10.1/aaa");
  // clearly different paper -> refuse (blank beats wrong)
  eq("doi.rejectWrong", B.pickBestDoi(de, [{ DOI: "10.1/bbb", title: ["A completely different paper on frogs"], author: [{ family: "Jones" }], issued: { "date-parts": [[1999]] } }]), null);
  // no candidates -> null
  eq("doi.empty", B.pickBestDoi(de, []), null);
  // picks the confident one out of several
  eq("doi.picksBest", B.pickBestDoi(de, [
    { DOI: "10.1/x", title: ["Unrelated"], author: [{ family: "X" }], issued: { "date-parts": [[2001]] } },
    { DOI: "10.1/right", title: ["Deep learning for image recognition"], author: [{ family: "Smith" }], issued: { "date-parts": [[2020]] } }
  ]), "10.1/right");
  // candidate has a subtitle, entry title fully contained, author + year agree -> accept
  var de2 = B.parseBib("@article{k, title={Learning image recognition}, author={Smith, Jane}, year={2020}}").entries[0];
  eq("doi.subtitleCorroborated", B.pickBestDoi(de2, [{ DOI: "10.1/ccc", title: ["Learning image recognition with deep nets"], author: [{ family: "Smith" }], issued: { "date-parts": [[2020]] } }]), "10.1/ccc");
  // same subset title but no author/year to corroborate -> refuse
  var de3 = B.parseBib("@misc{k, title={Learning image recognition}}").entries[0];
  eq("doi.subsetNoCorroboration", B.pickBestDoi(de3, [{ DOI: "10.1/ddd", title: ["Learning image recognition with deep nets in medicine"] }]), null);
  // author matches but the year is decades off -> refuse (likely a different work)
  eq("doi.yearMismatch", B.pickBestDoi(de2, [{ DOI: "10.1/eee", title: ["Learning image recognition with deep nets"], author: [{ family: "Smith" }], issued: { "date-parts": [[1990]] } }]), null);
  // a one-word title is too little to match on safely
  var de4 = B.parseBib("@article{k, title={Networks}, author={Smith, J}, year={2020}}").entries[0];
  eq("doi.needsTwoWords", B.pickBestDoi(de4, [{ DOI: "10.1/fff", title: ["Networks"], author: [{ family: "Smith" }], issued: { "date-parts": [[2020]] } }]), null);
  // stripTex cleans LaTeX before matching
  eq("stripTex.cleans", B.stripTex("The {DNA} \\emph{sequence}"), "The DNA sequence");

  return JSON.stringify({ pass: pass, fail: fail, total: pass + fail, failures: R });
})();
