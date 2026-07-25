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

  // ---- braced value ----
  var e1=B.parseBib("@article{key1, title={Hello World}}").entries;
  eq("parseBib.braced.count", e1.length, 1);
  eq("parseBib.braced.type", e1[0].type, "article");
  eq("parseBib.braced.key", e1[0].key, "key1");
  eq("parseBib.braced.field", B.field(e1[0],"title"), "Hello World");

  // ---- quoted value ----
  var e2=B.parseBib('@article{k2, title="Quoted"}').entries;
  eq("parseBib.quoted.field", B.field(e2[0],"title"), "Quoted");

  // ---- bare value ----
  var e3=B.parseBib("@article{k3, year=2020}").entries;
  eq("parseBib.bare.field", B.field(e3[0],"year"), "2020");

  // ---- nested braces several levels deep ----
  var e4=B.parseBib("@article{k4, title={a {b {c} d} e}}").entries;
  eq("parseBib.nested.field", B.field(e4[0],"title"), "a {b {c} d} e");

  // ---- string concatenation with # ----
  var e5=B.parseBib('@article{k5, title="Hello" # " World"}').entries;
  eq("parseBib.concat.quoted", B.field(e5[0],"title"), "Hello World");
  var e5b=B.parseBib("@article{k5b, author=abc # {def}}").entries;
  eq("parseBib.concat.mixed", B.field(e5b[0],"author"), "abcdef");

  // ---- @string meta ----
  var e6=B.parseBib("@string{pub = {ACME}}").entries;
  eq("parseBib.string.count", e6.length, 1);
  eq("parseBib.string.meta", e6[0].meta, true);
  eq("parseBib.string.type", e6[0].type, "string");
  eq("parseBib.string.fieldEmpty", B.field(e6[0],"pub"), "");

  // ---- @preamble meta ----
  var e7=B.parseBib('@preamble{"\\newcommand{\\x}{y}"}').entries;
  eq("parseBib.preamble.count", e7.length, 1);
  eq("parseBib.preamble.meta", e7[0].meta, true);
  eq("parseBib.preamble.type", e7[0].type, "preamble");

  // ---- @comment produces no entry ----
  var e8=B.parseBib("@comment{this {is} a comment}").entries;
  eq("parseBib.comment.count", e8.length, 0);
  var e8p=B.parseBib("@comment(paren comment)").entries;
  eq("parseBib.comment.paren.count", e8p.length, 0);

  // ---- paren-delimited entry ----
  var e9=B.parseBib("@article(k6, year=1999)").entries;
  eq("parseBib.paren.count", e9.length, 1);
  eq("parseBib.paren.key", e9[0].key, "k6");
  eq("parseBib.paren.field", B.field(e9[0],"year"), "1999");

  // ---- duplicate keys: both kept ----
  var e10=B.parseBib("@article{dup, year=1}@book{dup, year=2}").entries;
  eq("parseBib.dupkey.count", e10.length, 2);
  eq("parseBib.dupkey.k0", e10[0].key, "dup");
  eq("parseBib.dupkey.k1", e10[1].key, "dup");
  eq("parseBib.dupkey.t0", e10[0].type, "article");
  eq("parseBib.dupkey.t1", e10[1].type, "book");

  // ---- duplicate fields: first value kept ----
  var e11=B.parseBib("@article{k7, year=1, year=2}").entries;
  eq("parseBib.dupfield.value", B.field(e11[0],"year"), "1");
  eq("parseBib.dupfield.onlyone", e11[0].fields.length, 1);

  // ---- leading and trailing commas in field list ----
  var e12=B.parseBib("@article{k8, , year=2020, }").entries;
  eq("parseBib.commas.count", e12.length, 1);
  eq("parseBib.commas.field", B.field(e12[0],"year"), "2020");
  eq("parseBib.commas.onefield", e12[0].fields.length, 1);

  // ---- CRLF newlines ----
  var e13=B.parseBib("@article{k9,\r\n year={X}\r\n}").entries;
  eq("parseBib.crlf.field", B.field(e13[0],"year"), "X");

  // ---- mixed newlines ----
  var e14=B.parseBib("@article{k10,\n\r year={Y}\r}").entries;
  eq("parseBib.mixednl.field", B.field(e14[0],"year"), "Y");

  // ---- UTF-8 BOM at very start ----
  var e15=B.parseBib("﻿@article{k11, year=2021}").entries;
  eq("parseBib.bom.count", e15.length, 1);
  eq("parseBib.bom.key", e15[0].key, "k11");
  eq("parseBib.bom.field", B.field(e15[0],"year"), "2021");

  // ---- unicode in value and key ----
  var e16=B.parseBib("@article{clé, title={café ünï}}").entries;
  eq("parseBib.unicode.key", e16[0].key, "clé");
  eq("parseBib.unicode.value", B.field(e16[0],"title"), "café ünï");

  // ---- = inside a braced value (URL) ----
  var e17=B.parseBib("@article{k12, url={http://x.com/a?b=c&d=e}}").entries;
  eq("parseBib.urleq.field", B.field(e17[0],"url"), "http://x.com/a?b=c&d=e");

  // ---- # inside a braced value is preserved ----
  var e18=B.parseBib("@article{k12b, url={http://x.com/#frag}}").entries;
  eq("parseBib.hashinbrace.field", B.field(e18[0],"url"), "http://x.com/#frag");

  // ---- @ inside a value ----
  var e19=B.parseBib("@article{k13, note={email@domain}}").entries;
  eq("parseBib.atinvalue.count", e19.length, 1);
  eq("parseBib.atinvalue.field", B.field(e19[0],"note"), "email@domain");

  // ---- unbalanced braces must not crash; entry still recovered ----
  noThrow("parseBib.unbalanced.noThrow", function(){ B.parseBib("@article{k14, title={unbalanced"); });
  var e20=B.parseBib("@article{k14, title={unbalanced").entries;
  eq("parseBib.unbalanced.count", e20.length, 1);
  eq("parseBib.unbalanced.field", B.field(e20[0],"title"), "unbalanced");

  // ---- extra closing braces tolerated ----
  var e21=B.parseBib("@article{k15, title={ok}}}").entries;
  eq("parseBib.extrabrace.count", e21.length, 1);
  eq("parseBib.extrabrace.field", B.field(e21[0],"title"), "ok");

  // ---- empty string and pure junk => 0 entries ----
  eq("parseBib.empty.count", B.parseBib("").entries.length, 0);
  eq("parseBib.junk.count", B.parseBib("just some random text without at signs").entries.length, 0);

  // ---- several entries back to back ----
  var e22=B.parseBib("@article{a1,year=1}@book{b1,year=2}@misc{c1,year=3}").entries;
  eq("parseBib.multi.count", e22.length, 3);
  eqJSON("parseBib.multi.keys", [e22[0].key,e22[1].key,e22[2].key], ["a1","b1","c1"]);
  eqJSON("parseBib.multi.types", [e22[0].type,e22[1].type,e22[2].type], ["article","book","misc"]);

  // ---- whitespace-only field value collapses to empty ----
  var e23=B.parseBib("@article{k16, title={   }}").entries;
  eq("parseBib.wsonly.field", B.field(e23[0],"title"), "");
  eq("parseBib.wsonly.hasfield", e23[0].fields.length, 1);

  // ---- a very long value ----
  var longv=new Array(5001).join("x");
  var e24=B.parseBib("@article{k17, title={"+longv+"}}").entries;
  eq("parseBib.long.len", B.field(e24[0],"title").length, 5000);
  eq("parseBib.long.eq", B.field(e24[0],"title"), longv);

  // ---- type lowercased, key case preserved, field name lowercased ----
  var e25=B.parseBib("@Article{MyKey, Year=2020}").entries;
  eq("parseBib.case.type", e25[0].type, "article");
  eq("parseBib.case.key", e25[0].key, "MyKey");
  eq("parseBib.case.fieldlower", B.field(e25[0],"year"), "2020");
  eq("parseBib.case.fieldOrigMiss", B.field(e25[0],"Year"), "");

  // ---- missing field returns "" ----
  eq("parseBib.missing.field", B.field(e1[0],"nonexistent"), "");

  // ---- normal entry has no meta flag ----
  ok("parseBib.normal.nometa", e1[0].meta!==true, "got="+JSON.stringify(e1[0].meta));

  // ---- multiline braced value collapses internal whitespace ----
  var e26=B.parseBib("@article{k18, abstract={line one\n   line two}}").entries;
  eq("parseBib.multiline.field", B.field(e26[0],"abstract"), "line one line two");

  // ---- meta entry then normal entry: counts and flags ----
  var e27=B.parseBib("@string{j={JCP}}@article{k19, year=2000}").entries;
  eq("parseBib.mixed.count", e27.length, 2);
  eq("parseBib.mixed.metaflag", e27[0].meta, true);
  ok("parseBib.mixed.normalnometa", e27[1].meta!==true, "got="+JSON.stringify(e27[1].meta));
  eq("parseBib.mixed.normalfield", B.field(e27[1],"year"), "2000");

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
