(function(){
  if(!window.BIB) return JSON.stringify({pass:0,fail:1,total:1,failures:[{name:"window.BIB missing",detail:""}]});
  var B=window.BIB, R=[], pass=0, fail=0;
  function rec(n,d){ fail++; R.push({name:n,detail:String(d===undefined?"":d)}); }
  function ok(n,c,d){ if(c) pass++; else rec(n,d); }
  function eq(n,a,b){ ok(n,a===b,"got="+JSON.stringify(a)+" want="+JSON.stringify(b)); }
  function mk(bib){ return B.parseBib(bib).entries[0]; }

  // ---- extractUrl ----
  eq("url.extract.bare", B.extractUrl("https://example.com"), "https://example.com");
  eq("url.extract.urlmacro", B.extractUrl("\\url{https://example.com}"), "https://example.com");
  eq("url.extract.href", B.extractUrl("\\href{https://example.com}{link}"), "https://example.com");
  eq("url.extract.embedded", B.extractUrl("Available at https://example.com."), "https://example.com");
  eq("url.extract.www", B.extractUrl("www.example.com"), "www.example.com");
  eq("url.extract.ftp", B.extractUrl("ftp://files.example.com/a"), "ftp://files.example.com/a");
  eq("url.extract.parenWrapped", B.extractUrl("(https://example.com)"), "https://example.com");
  eq("url.extract.none", B.extractUrl("just some text"), "");
  eq("url.extract.empty", B.extractUrl(""), "");

  // ---- looksLikeUrl ----
  eq("url.looks.https", B.looksLikeUrl("https://x.com"), true);
  eq("url.looks.www", B.looksLikeUrl("www.x.com"), true);
  eq("url.looks.ftp", B.looksLikeUrl("ftp://x.com/a"), true);
  eq("url.looks.plain", B.looksLikeUrl("example.com"), false);
  eq("url.looks.text", B.looksLikeUrl("hello world"), false);
  eq("url.looks.hasSpace", B.looksLikeUrl("https://x.com/a b"), false);

  // ---- stripUrlFrom ----
  eq("url.strip.macroOnly", B.stripUrlFrom("\\url{https://x.com}"), "");
  eq("url.strip.connectorPhrase", B.stripUrlFrom("Available at \\url{https://x.com}"), "");
  eq("url.strip.connectorOnlineAt", B.stripUrlFrom("Online at https://x.com"), "");
  eq("url.strip.keepsText", B.stripUrlFrom("See our site https://x.com for details"), "See our site for details");
  eq("url.strip.noUrl", B.stripUrlFrom("preprint version"), "preprint version");
  eq("url.strip.empty", B.stripUrlFrom(""), "");

  // ---- urlToField (drop from howpublished/note, no duplicates) ----
  var a1=mk("@misc{k, title={T}, howpublished={\\url{https://x.com}}}");
  eq("url.toField.returnsTrue", B.urlToField(a1), true);
  eq("url.toField.setsUrl", B.field(a1,"url"), "https://x.com");
  eq("url.toField.dropsHow", B.field(a1,"howpublished"), "");

  var a2=mk("@article{k, title={T}, note={https://x.com}}");
  B.urlToField(a2);
  eq("url.toField.fromNote", B.field(a2,"url"), "https://x.com");
  eq("url.toField.dropsNote", B.field(a2,"note"), "");

  var a3=mk("@misc{k, url={https://x.com}, howpublished={\\url{https://x.com}}}");
  B.urlToField(a3);
  eq("url.toField.dedupHow", B.field(a3,"howpublished"), "");
  eq("url.toField.keepsUrl", B.field(a3,"url"), "https://x.com");

  var a4=mk("@article{k, title={T}}");
  eq("url.toField.noUrlReturnsFalse", B.urlToField(a4), false);
  eq("url.toField.noUrlUnchanged", B.field(a4,"url"), "");

  var a5=mk("@misc{k, howpublished={Available at \\url{https://x.com}}}");
  B.urlToField(a5);
  eq("url.toField.textPlusUrl.url", B.field(a5,"url"), "https://x.com");
  eq("url.toField.textPlusUrl.dropsConnector", B.field(a5,"howpublished"), "");

  var a6=mk("@misc{k, howpublished={See our page https://x.com for details}}");
  B.urlToField(a6);
  eq("url.toField.leftoverText", B.field(a6,"howpublished"), "See our page for details");
  eq("url.toField.leftoverText.url", B.field(a6,"url"), "https://x.com");

  var am=mk("@string{x={y}}");
  eq("url.toField.metaFalse", B.urlToField(am), false);

  // ---- urlToHowpublished (misc/online -> howpublished, else note; drop url) ----
  var b1=mk("@misc{k, title={T}, url={https://x.com}}");
  eq("url.toHow.returnsTrue", B.urlToHowpublished(b1), true);
  eq("url.toHow.miscHow", B.field(b1,"howpublished"), "\\url{https://x.com}");
  eq("url.toHow.dropsUrl", B.field(b1,"url"), "");

  var b2=mk("@article{k, title={T}, url={https://x.com}}");
  B.urlToHowpublished(b2);
  eq("url.toHow.articleNote", B.field(b2,"note"), "\\url{https://x.com}");
  eq("url.toHow.articleDropsUrl", B.field(b2,"url"), "");

  var b3=mk("@article{k, note={Preprint}, url={https://x.com}}");
  B.urlToHowpublished(b3);
  eq("url.toHow.appendsExistingNote", B.field(b3,"note"), "Preprint \\url{https://x.com}");

  var b4=mk("@online{k, title={T}, url={https://x.com}}");
  B.urlToHowpublished(b4);
  eq("url.toHow.onlineHow", B.field(b4,"howpublished"), "\\url{https://x.com}");

  var b5=mk("@misc{k, title={T}}");
  eq("url.toHow.noUrlFalse", B.urlToHowpublished(b5), false);

  var b6=mk("@misc{k, url={https://x.com}, howpublished={\\url{https://x.com}}}");
  B.urlToHowpublished(b6); // already present -> should not duplicate, just drop url
  eq("url.toHow.noDuplicate", B.field(b6,"howpublished"), "\\url{https://x.com}");
  eq("url.toHow.noDuplicate.dropsUrl", B.field(b6,"url"), "");

  // ---- formatEntry round-trip ----
  var c1=mk("@misc{k, title={T}, url={https://x.com}}");
  B.urlToHowpublished(c1);
  var out1=B.formatEntry(c1,{lower:true,strip:true,sortFields:false});
  ok("url.format.howSerialized", /howpublished\s*=\s*\{\\url\{https:\/\/x\.com\}\}/.test(out1), out1);
  ok("url.format.noBareUrlLine", !/\burl\s*=\s*\{/.test(out1), out1);

  var c2=mk("@misc{k, title={T}, howpublished={\\url{https://x.com}}}");
  B.urlToField(c2);
  var out2=B.formatEntry(c2,{lower:true,strip:true,sortFields:false});
  ok("url.format.urlSerialized", /url\s*=\s*\{https:\/\/x\.com\}/.test(out2), out2);
  ok("url.format.noHowpublished", out2.indexOf("howpublished")<0, out2);

  // ---- end-to-end: mixed .bib, both directions ----
  var bib="@misc{web, title={Dataset}, howpublished={\\url{https://data.example.com/set}}}\n@article{art, title={Paper}, journal={J}, url={https://doi.org/10.1/x}}";
  var es=B.parseBib(bib).entries, moved=0;
  es.forEach(function(e){ if(B.urlToField(e)) moved++; });
  eq("url.e2e.toField.movedCount", moved, 1);
  eq("url.e2e.toField.webUrl", B.field(es[0],"url"), "https://data.example.com/set");
  eq("url.e2e.toField.webNoHow", B.field(es[0],"howpublished"), "");
  eq("url.e2e.toField.artUrlKept", B.field(es[1],"url"), "https://doi.org/10.1/x");

  var es2=B.parseBib(bib).entries, moved2=0;
  es2.forEach(function(e){ if(B.urlToHowpublished(e)) moved2++; });
  eq("url.e2e.toHow.movedCount", moved2, 1);
  eq("url.e2e.toHow.artNote", B.field(es2[1],"note"), "\\url{https://doi.org/10.1/x}");
  eq("url.e2e.toHow.artNoUrl", B.field(es2[1],"url"), "");
  eq("url.e2e.toHow.webUnchanged", B.field(es2[0],"howpublished"), "\\url{https://data.example.com/set}");

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
