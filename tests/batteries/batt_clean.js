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

  var ALL={dedupe:true,sort:true,strip:true,brace:true,lower:true,sortFields:false};

  // --- return shape & stats keys ---
  var r0=B.clean("@article{k,title={X},journal={J},year={2020},author={A}}",ALL);
  ok("clean.returns_text_string", typeof r0.text==="string","text not string");
  ok("clean.returns_entries_array", Array.isArray(r0.entries),"entries not array");
  ok("clean.returns_stats_object", r0.stats&&typeof r0.stats==="object","no stats");
  eqJSON("clean.stats_keys", Object.keys(r0.stats).sort(), ["allcaps","braced","issues","merged","monthsFixed","nEntries","possibleDupes"]);
  eq("clean.nEntries_one", r0.stats.nEntries, 1);

  // --- empty / whitespace input ---
  var re=B.clean("",ALL);
  eq("clean.empty_text", re.text, "");
  eq("clean.empty_nEntries", re.stats.nEntries, 0);
  eqJSON("clean.empty_issues", re.stats.issues, []);
  noThrow("clean.whitespace_only_no_throw", function(){ B.clean("   \n\t  ",ALL); });

  // --- nEntries counts non-meta only ---
  var rn=B.clean("@string{p={ACME}}\n@article{k,title={X},journal={J},year={2020},author={A}}",ALL);
  eq("clean.nEntries_excludes_meta", rn.stats.nEntries, 1);

  // --- field names lowercased (by parser, surfaced in output) ---
  var rl=B.clean("@article{k, TITLE={X}, Journal={J}, YEAR={2020}, Author={A}}",{strip:false});
  ok("clean.fieldname_lowered_title", rl.text.indexOf("title")>=0,"no lower title");
  ok("clean.fieldname_no_uppercase", rl.text.indexOf("TITLE")<0 && rl.text.indexOf("Author")<0,"uppercase field name remained");

  // --- type lowercased even with lower:false (parser normalizes) ---
  var rt=B.clean("@ARTICLE{k,title={X},journal={J},year={2020},author={A}}",{lower:false});
  ok("clean.type_lowercased", rt.text.indexOf("@article{")>=0,"type not lowercased");
  ok("clean.type_no_uppercase", rt.text.indexOf("@ARTICLE")<0,"uppercase type remained");

  // --- strip empty fields ---
  var rs1=B.clean("@article{k, title={X}, note={   }, year={2020}}",{strip:true});
  ok("clean.strip_removes_empty", rs1.text.indexOf("note")<0,"empty note not stripped");
  var rs2=B.clean("@article{k, title={X}, note={   }, year={2020}}",{strip:false});
  ok("clean.strip_off_keeps_empty", rs2.text.indexOf("note")>=0,"empty note removed with strip off");

  // --- month name -> macro, unbraced ---
  var rm=B.clean("@article{k, title={X}, month={January}, year={2020}}",{});
  ok("clean.month_january_to_jan", rm.text.indexOf("= jan")>=0,"month not normalized");
  ok("clean.month_unbraced", rm.text.indexOf("{jan}")<0,"month macro was braced");
  eq("clean.monthsFixed_stat", rm.stats.monthsFixed, 1);
  var rm2=B.clean("@article{k, title={X}, month={1}, year={2020}}",{});
  ok("clean.month_numeric_to_macro", rm2.text.indexOf("= jan")>=0,"numeric month not normalized");

  // --- ALL-CAPS title flagged, not transformed ---
  var rc=B.clean("@article{k, title={PARALLEL COMPUTING SYSTEMS}, journal={J}, year={2020}, author={A}}",{brace:true});
  eq("clean.allcaps_flagged", rc.stats.allcaps, 1);
  ok("clean.allcaps_not_transformed", rc.text.indexOf("PARALLEL COMPUTING SYSTEMS")>=0,"allcaps title altered");
  eq("clean.allcaps_not_braced", rc.stats.braced, 0);
  ok("clean.allcaps_issue_note", rc.stats.issues.some(function(x){return /ALL-CAPS/.test(x.msg);}),"no allcaps note");

  // --- acronym bracing when brace:true (acronym survives) ---
  var rb=B.clean("@article{k2, title={The DNA of Systems}, journal={J}, year={2020}, author={A}}",{brace:true});
  ok("clean.brace_protects_acronym", rb.text.indexOf("{DNA}")>=0,"acronym not braced");
  eq("clean.brace_stat", rb.stats.braced, 1);
  var rb2=B.clean("@article{k2, title={The DNA of Systems}, journal={J}, year={2020}, author={A}}",{brace:false});
  ok("clean.brace_off_no_bracing", rb2.text.indexOf("{DNA}")<0,"braced with brace off");
  ok("clean.brace_off_title_intact", rb2.text.indexOf("The DNA of Systems")>=0,"title altered with brace off");

  // --- sort by key ---
  var srcSort="@article{zebra,title={Z},journal={J},year={2020},author={A}}\n@article{apple,title={A},journal={J},year={2019},author={B}}";
  var rso=B.clean(srcSort,{sort:true});
  ok("clean.sort_by_key", rso.text.indexOf("apple")<rso.text.indexOf("zebra"),"not sorted by key");
  var rns=B.clean(srcSort,{sort:false});
  ok("clean.sort_off_preserves_order", rns.text.indexOf("zebra")<rns.text.indexOf("apple"),"order changed with sort off");

  // --- metas sort first ---
  var rmf=B.clean("@article{zzz,title={X},journal={J},year={2020},author={A}}\n@string{p={ACME}}",{sort:true});
  ok("clean.sort_metas_first", rmf.text.indexOf("@string")<rmf.text.indexOf("@article"),"meta not first after sort");

  // --- no trailing comma on last field ---
  var rtc=B.clean("@article{k,title={X},year={2020}}",{strip:true});
  ok("clean.no_trailing_comma", rtc.text.indexOf(",\n}")<0,"trailing comma present");

  // --- @string meta passthrough survives ---
  var rstr=B.clean("@string{pub = {ACME Press}}\n@article{k,title={X},journal={J},year={2020},author={A}}",{});
  ok("clean.string_passthrough", rstr.text.indexOf("@string{pub = {ACME Press}}")>=0,"@string not preserved");

  // --- @comment is dropped ---
  var rcom=B.clean("@comment{ignore this line}\n@article{k,title={X},journal={J},year={2020},author={A}}",{});
  ok("clean.comment_dropped", rcom.text.indexOf("ignore this line")<0,"comment not dropped");
  eq("clean.comment_nEntries", rcom.stats.nEntries, 1);

  // --- dedupe merges by key ---
  var dupSrc="@article{dup,title={A},journal={J},year={2020},author={X}}\n@article{dup,title={A},journal={J},year={2020},author={X},volume={5}}";
  var rd=B.clean(dupSrc,{dedupe:true});
  eq("clean.dedupe_merged_stat", rd.stats.merged, 1);
  eq("clean.dedupe_nEntries", rd.stats.nEntries, 1);
  ok("clean.dedupe_fills_fields", rd.text.indexOf("volume")>=0,"merge did not fill missing field");
  var rdo=B.clean(dupSrc,{dedupe:false});
  eq("clean.dedupe_off_merged", rdo.stats.merged, 0);
  eq("clean.dedupe_off_nEntries", rdo.stats.nEntries, 2);

  // --- fuzzy possible duplicates flagged, never merged ---
  var fz="@article{a1,title={Deep Learning For Image Recognition},journal={J},year={2020},author={Smith, J}}\n@article{a2,title={Deep Learning For Image Recognition Systems},journal={K},year={2021},author={Doe, D}}";
  var rfz=B.clean(fz,{dedupe:true});
  ok("clean.possibleDupes_flagged", rfz.stats.possibleDupes>=1,"fuzzy dup not flagged");
  eq("clean.possibleDupes_not_merged", rfz.stats.nEntries, 2);

  // --- validation issues for missing required fields ---
  var rvi=B.clean("@article{m,title={X},year={2020}}",{});
  ok("clean.issues_missing_field", rvi.stats.issues.some(function(x){return /journal/.test(x.msg);}),"missing journal not reported");

  // --- malformed / adversarial inputs do not throw ---
  noThrow("clean.malformed_truncated", function(){ B.clean("@article{bad, title = ",ALL); });
  noThrow("clean.malformed_no_braces", function(){ B.clean("@@@ not bibtex at all",ALL); });
  noThrow("clean.malformed_missing_eq", function(){ B.clean("@article{k, title {X}, year={2020}}",ALL); });

  // --- unicode preserved ---
  var ru=B.clean("@article{u,title={Café Müller Über},journal={J},year={2020},author={Ångström, K}}",{brace:true});
  ok("clean.unicode_title_preserved", ru.text.indexOf("Café Müller Über")>=0,"unicode title lost");
  ok("clean.unicode_author_preserved", ru.text.indexOf("Ångström, K")>=0,"unicode author lost");

  // --- idempotence: clean twice == clean once ---
  var idemSrc="@ARTICLE{k, TITLE={The DNA of Systems}, Month={January}, Journal={J}, Year={2020}, Author={Smith, John}}";
  var once=B.clean(idemSrc,ALL).text;
  var twice=B.clean(once,ALL).text;
  eq("clean.idempotent", twice, once);

  // --- round-trip: parseBib of cleaned text preserves author/title/year ---
  var rtSrc="@article{rt, title={Hello World}, author={Smith, John}, year={2020}, journal={J}}";
  var cleaned=B.clean(rtSrc,{strip:true,brace:true}).text;
  var pp=B.parseBib(cleaned).entries.filter(function(e){return !e.meta;})[0];
  eq("clean.roundtrip_title", B.field(pp,"title"), "Hello World");
  eq("clean.roundtrip_author", B.field(pp,"author"), "Smith, John");
  eq("clean.roundtrip_year", B.field(pp,"year"), "2020");

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
