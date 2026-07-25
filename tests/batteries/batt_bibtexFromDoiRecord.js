(function(){
  if(!window.BIB) return JSON.stringify({pass:0,fail:1,total:1,failures:[{name:"window.BIB missing",detail:""}]});
  var B=window.BIB, R=[], pass=0, fail=0;
  function rec(n,d){ fail++; R.push({name:n,detail:String(d===undefined?"":d)}); }
  function ok(n,c,d){ if(c) pass++; else rec(n,d); }
  function eq(n,a,b){ ok(n,a===b,"got="+JSON.stringify(a)+" want="+JSON.stringify(b)); }
  function eqJSON(n,a,b){ var x=JSON.stringify(a),y=JSON.stringify(b); ok(n,x===y,"got="+x+" want="+y); }
  function throws(n,fn){ var t=false; try{fn();}catch(e){t=true;} ok(n,t,"expected throw"); }
  function noThrow(n,fn){ var t=null; try{fn();}catch(e){t=String(e);} ok(n,t===null,"threw "+t); }
  function arr(s){ return Array.from(s).sort(); }

  // ---- Realistic multi-field @article (Crossref x-bibtex shape) ----
  var mainBib = "@article{Doe_2020,\n"+
    "  doi = {10.1234/example.doi},\n"+
    "  url = {https://doi.org/10.1234/example.doi},\n"+
    "  year = {2020},\n"+
    "  month = jan,\n"+
    "  publisher = {Example Publisher},\n"+
    "  volume = {12},\n"+
    "  number = {3},\n"+
    "  pages = {45--67},\n"+
    "  author = {John Doe and Jane Smith},\n"+
    "  title = {A Study of Things},\n"+
    "  journal = {Journal of Examples}\n"+
    "}";
  var m = B.bibtexFromDoiRecord(mainBib);

  ok("bibtexFromDoiRecord.article_not_null", m!==null, "got null");
  eq("bibtexFromDoiRecord.article_type", m && m.type, "article");
  eq("bibtexFromDoiRecord.article_key", m && m.key, "Doe_2020");
  eq("bibtexFromDoiRecord.article_no_meta", m && (m.meta===undefined), true);
  eq("bibtexFromDoiRecord.field_title", B.field(m,"title"), "A Study of Things");
  eq("bibtexFromDoiRecord.field_author", B.field(m,"author"), "John Doe and Jane Smith");
  eq("bibtexFromDoiRecord.field_doi", B.field(m,"doi"), "10.1234/example.doi");
  eq("bibtexFromDoiRecord.field_year", B.field(m,"year"), "2020");
  eq("bibtexFromDoiRecord.field_journal", B.field(m,"journal"), "Journal of Examples");
  eq("bibtexFromDoiRecord.field_pages", B.field(m,"pages"), "45--67");
  eq("bibtexFromDoiRecord.field_volume", B.field(m,"volume"), "12");
  eq("bibtexFromDoiRecord.field_bareword_month", B.field(m,"month"), "jan");
  eq("bibtexFromDoiRecord.field_missing_empty", B.field(m,"nosuchfield"), "");

  // ---- Empty / whitespace / garbage -> null ----
  eq("bibtexFromDoiRecord.empty_null", B.bibtexFromDoiRecord(""), null);
  eq("bibtexFromDoiRecord.whitespace_null", B.bibtexFromDoiRecord("   \n\t "), null);
  eq("bibtexFromDoiRecord.garbage_null", B.bibtexFromDoiRecord("this is not bibtex at all"), null);
  eq("bibtexFromDoiRecord.at_no_body_null", B.bibtexFromDoiRecord("@article"), null);
  eq("bibtexFromDoiRecord.at_no_delim_null", B.bibtexFromDoiRecord("@article no braces here"), null);

  // ---- Meta entries ignored ----
  var s1 = B.bibtexFromDoiRecord("@string{foo = {Bar}}\n@article{K1, title={T1}, doi={10.1/x}}");
  ok("bibtexFromDoiRecord.leading_string_skipped", s1!==null && s1.type==="article", "got="+JSON.stringify(s1));
  eq("bibtexFromDoiRecord.leading_string_key", s1 && s1.key, "K1");
  var c1 = B.bibtexFromDoiRecord("@comment{ignore me @article{X,y=z}}\n@article{K2, title={T2}}");
  ok("bibtexFromDoiRecord.leading_comment_skipped", c1!==null && c1.type==="article", "got="+JSON.stringify(c1));
  eq("bibtexFromDoiRecord.leading_comment_key", c1 && c1.key, "K2");
  eq("bibtexFromDoiRecord.only_string_null", B.bibtexFromDoiRecord("@string{foo = {Bar}}"), null);
  eq("bibtexFromDoiRecord.only_comment_null", B.bibtexFromDoiRecord("@comment{just a note}"), null);
  eq("bibtexFromDoiRecord.only_preamble_null", B.bibtexFromDoiRecord("@preamble{\"\\newcommand{}\"}"), null);

  // ---- First non-meta entry only ----
  var two = B.bibtexFromDoiRecord("@article{First, title={One}}\n@book{Second, title={Two}}");
  eq("bibtexFromDoiRecord.first_entry_key", two && two.key, "First");
  eq("bibtexFromDoiRecord.first_entry_type", two && two.type, "article");

  // ---- Braces / nesting / DOI url form ----
  var nb = B.bibtexFromDoiRecord("@article{N, title={A {DNA} Study}, doi={10.5555/nested}}");
  eq("bibtexFromDoiRecord.nested_braces_title", B.field(nb,"title"), "A {DNA} Study");
  eq("bibtexFromDoiRecord.nested_braces_doi", B.field(nb,"doi"), "10.5555/nested");

  // ---- Paren-delimited entry ----
  var pr = B.bibtexFromDoiRecord("@article(P, title={Paren Style}, year={1999})");
  ok("bibtexFromDoiRecord.paren_not_null", pr!==null, "got null");
  eq("bibtexFromDoiRecord.paren_title", B.field(pr,"title"), "Paren Style");

  // ---- Quoted values & concatenation ----
  var qv = B.bibtexFromDoiRecord("@article{Q, title = \"Quoted Title\", author=\"A\" # \"B\"}");
  eq("bibtexFromDoiRecord.quoted_title", B.field(qv,"title"), "Quoted Title");
  eq("bibtexFromDoiRecord.concat_author", B.field(qv,"author"), "AB");

  // ---- Whitespace collapse inside value ----
  var wsv = B.bibtexFromDoiRecord("@article{W, title={A    Study\n  Of   Space}}");
  eq("bibtexFromDoiRecord.value_ws_collapse", B.field(wsv,"title"), "A Study Of Space");

  // ---- Unicode ----
  var uni = B.bibtexFromDoiRecord("@article{U, title={Étude sur les naïve résumés}, author={Müller, Jörg}}");
  eq("bibtexFromDoiRecord.unicode_title", B.field(uni,"title"), "Étude sur les naïve résumés");
  eq("bibtexFromDoiRecord.unicode_author", B.field(uni,"author"), "Müller, Jörg");

  // ---- Case-insensitive entry type ----
  var uc = B.bibtexFromDoiRecord("@ARTICLE{C, title={Caps}}");
  eq("bibtexFromDoiRecord.type_lowercased", uc && uc.type, "article");

  // ---- No-throw robustness on odd/malformed inputs ----
  noThrow("bibtexFromDoiRecord.nothrow_garbage", function(){ B.bibtexFromDoiRecord("###@@@!!!"); });
  noThrow("bibtexFromDoiRecord.nothrow_unterminated", function(){ B.bibtexFromDoiRecord("@article{K, title={unterminated"); });
  noThrow("bibtexFromDoiRecord.nothrow_empty", function(){ B.bibtexFromDoiRecord(""); });

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
