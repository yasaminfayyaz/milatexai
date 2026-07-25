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
  var OPT={dedupe:true,sort:true,strip:true,brace:true,lower:true};
  function getE(res,key){ return B.parseBib(res.text).entries.find(function(e){return !e.meta && e.key===key;}); }
  function fv(res,key,name){ var e=getE(res,key); return e?B.field(e,name):"__NOENTRY__"; }

  // ===== Scenario A: Zotero-style messy export, duplicate by DOI, ALL-CAPS title, acronyms, months =====
  var bibA=[
    "@STRING{acl = {Association for Computational Linguistics}}",
    "",
    "@article{smith2020,",
    "  Title = {DEEP LEARNING FOR NLP},",
    "  author = {John Smith and Jane Doe},",
    "  JOURNAL = {Nature},",
    "  year = {2020},",
    "  doi = {10.1000/ABC},",
    "  month = {March}",
    "}",
    "",
    "@article{smith2020dup,",
    "  title = {Deep learning for NLP},",
    "  author = {John Smith and Jane Doe},",
    "  journal = {Nature},",
    "  year = {2020},",
    "  doi = {https://doi.org/10.1000/abc},",
    "  volume = {12}",
    "}",
    "",
    "@inproceedings{jones,",
    "  title = {A Study of DNA and RNA using NASA data},",
    "  author = {Emile Zola},",
    "  booktitle = {Proc of ACL},",
    "  year = {2019},",
    "  month = {jan}",
    "}"
  ].join("\n");
  var resA=B.clean(bibA,OPT);
  eq("e2e_bib.A_nEntries", resA.stats.nEntries, 2);
  eq("e2e_bib.A_merged", resA.stats.merged, 1);
  eq("e2e_bib.A_allcaps", resA.stats.allcaps, 1);
  eq("e2e_bib.A_braced", resA.stats.braced, 1);
  eq("e2e_bib.A_monthsFixed", resA.stats.monthsFixed, 2);
  eq("e2e_bib.A_possibleDupes", resA.stats.possibleDupes, 0);
  eq("e2e_bib.A_allcaps_title_preserved", fv(resA,"smith2020","title"), "DEEP LEARNING FOR NLP");
  eq("e2e_bib.A_merged_volume", fv(resA,"smith2020","volume"), "12");
  eq("e2e_bib.A_month_macro", fv(resA,"smith2020","month"), "mar");
  ok("e2e_bib.A_acronym_dna", fv(resA,"jones","title").indexOf("{DNA}")>=0, fv(resA,"jones","title"));
  ok("e2e_bib.A_acronym_nasa", fv(resA,"jones","title").indexOf("{NASA}")>=0, fv(resA,"jones","title"));
  ok("e2e_bib.A_no_trailing_comma", resA.text.indexOf(",\n}")===-1, resA.text);
  ok("e2e_bib.A_type_lowercased", resA.text.indexOf("@article{")>=0 && resA.text.indexOf("@ARTICLE")===-1, resA.text);
  ok("e2e_bib.A_string_macro_preserved", resA.text.indexOf("@string{")>=0, resA.text);
  ok("e2e_bib.A_issues_array_nonempty", Array.isArray(resA.stats.issues) && resA.stats.issues.length>0, JSON.stringify(resA.stats.issues));
  ok("e2e_bib.A_roundtrip_dup_key_gone", getE(resA,"smith2020dup")===undefined, "dup should be merged away");

  // ===== Scenario B: fuzzy near-duplicate titles, different keys, no DOI => flagged not merged =====
  var bibB=[
    "@article{a1, title={Neural Networks for Image Classification}, author={Alan Turing}, journal={JMLR}, year={2018}}",
    "@article{a2, title={Neural Networks for Image Classification and Beyond}, author={Alan Turing}, journal={arXiv}, year={2018}}"
  ].join("\n");
  var resB=B.clean(bibB,OPT);
  eq("e2e_bib.B_possibleDupes", resB.stats.possibleDupes, 1);
  eq("e2e_bib.B_merged", resB.stats.merged, 0);
  eq("e2e_bib.B_nEntries", resB.stats.nEntries, 2);
  ok("e2e_bib.B_both_present", getE(resB,"a1")!==undefined && getE(resB,"a2")!==undefined, resB.text);

  // ===== Scenario C: empty input =====
  var resC=B.clean("",OPT);
  eq("e2e_bib.C_empty_text", resC.text, "");
  eq("e2e_bib.C_empty_nEntries", resC.stats.nEntries, 0);
  eq("e2e_bib.C_empty_entries_len", resC.entries.length, 0);
  noThrow("e2e_bib.C_empty_noThrow", function(){ B.clean("",OPT); });

  // ===== Scenario D: adversarial / malformed input =====
  var bibD=[
    "@article{,",
    "  title = {Orphan No Key},",
    "  year = {2001}",
    "}",
    "@misc{brokenfield",
    "  title {NoEquals}",
    "}",
    "@article{good, title={Fine}, author={A B}, journal={J}, year={2000}}"
  ].join("\n");
  var resD;
  noThrow("e2e_bib.D_malformed_noThrow", function(){ resD=B.clean(bibD,OPT); });
  eq("e2e_bib.D_good_survives_journal", fv(resD,"good","journal"), "J");
  ok("e2e_bib.D_reparse_clean", B.parseBib(resD.text).entries.filter(function(e){return !e.meta;}).length>=1, resD.text);

  // ===== Scenario E: unicode authors, already-braced acronym, numeric month macro =====
  var bibE=[
    "@string{nature = {Nature Publishing}}",
    "@article{uni, title={On {CO2} Emissions}, author={Emile Zoric and Jose Pena}, journal={X}, year={2021}, month={12}}"
  ].join("\n");
  var resE=B.clean(bibE,OPT);
  eq("e2e_bib.E_author_roundtrip", fv(resE,"uni","author"), "Emile Zoric and Jose Pena");
  eq("e2e_bib.E_month_num_to_dec", fv(resE,"uni","month"), "dec");
  eq("e2e_bib.E_no_double_brace", resE.stats.braced, 0);
  ok("e2e_bib.E_co2_preserved", fv(resE,"uni","title").indexOf("{CO2}")>=0, fv(resE,"uni","title"));

  // ===== Scenario F: strip empty / whitespace-only fields =====
  var bibF="@article{ent, title={T}, author={}, journal={J}, year={2000}, note={  }}";
  var resF=B.clean(bibF,OPT);
  eq("e2e_bib.F_empty_author_stripped", fv(resF,"ent","author"), "");
  ok("e2e_bib.F_note_removed", resF.text.indexOf("note")===-1, resF.text);
  eq("e2e_bib.F_kept_title", fv(resF,"ent","title"), "T");

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
