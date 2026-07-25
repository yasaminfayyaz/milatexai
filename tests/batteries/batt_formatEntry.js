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
  var O={};
  function ent(bt){ return B.parseBib(bt).entries[0]; }

  // ---- basic full-string formatting ----
  var e1=ent("@article{key1, author={John Doe}, title={A Title}, year={2020}}");
  var s1=B.formatEntry(e1,O);
  var exp1="@article{key1,\n  author = {John Doe},\n  title  = {A Title},\n  year   = {2020}\n}";
  eq("formatEntry.basic.full", s1, exp1);
  ok("formatEntry.basic.startsWithTypeKey", s1.indexOf("@article{key1,")===0, s1);
  ok("formatEntry.basic.twoSpaceIndent", s1.indexOf("\n  author = {")>=0, s1);
  ok("formatEntry.basic.bracedValue", s1.indexOf("{A Title}")>=0, s1);
  ok("formatEntry.basic.closingBraceOnOwnLine", /\n}$/.test(s1), s1);
  ok("formatEntry.basic.padAlignment", s1.indexOf("  title  = {")>=0, s1);
  ok("formatEntry.basic.equalsSeparator", s1.indexOf(" = {")>=0, s1);

  // ---- no trailing comma after last field ----
  ok("formatEntry.noTrailingComma.lastFieldNoComma", /\{2020\}\n}$/.test(s1), s1);
  ok("formatEntry.noTrailingComma.notCommaBeforeClose", s1.indexOf(",\n}")===-1, s1);
  ok("formatEntry.commaBetweenFields", s1.indexOf("},\n  title")>=0, s1);

  // ---- key & type placement ----
  var e2=ent("@InProceedings{mykey2, note={n}}");
  var s2=B.formatEntry(e2,O);
  ok("formatEntry.type.lowercasedByParser", s2.indexOf("@inproceedings{mykey2,")===0, s2);
  var e3=ent("@misc{, title={x}}");
  var s3=B.formatEntry(e3,O);
  ok("formatEntry.emptyKey.commaAfterEmptyKey", s3.indexOf("@misc{,")===0, s3);

  // ---- empty field list ----
  var e4=ent("@misc{onlykey}");
  eq("formatEntry.emptyFields.exact", B.formatEntry(e4,O), "@misc{onlykey,\n\n}");

  // ---- meta (@string / @preamble) ----
  var m1=ent("@string{acm = {ACM Press}}");
  eq("formatEntry.meta.stringExact", B.formatEntry(m1,O), "@string{acm = {ACM Press}}");
  ok("formatEntry.meta.isMetaFlag", m1.meta===true, m1);
  var m2=ent("@string(ieee = {IEEE})");
  eq("formatEntry.meta.parenNormalizedToBrace", B.formatEntry(m2,O), "@string{ieee = {IEEE}}");
  noThrow("formatEntry.meta.optUndefinedSafe", function(){ B.formatEntry(m1); });

  // ---- non-meta requires opt (adversarial) ----
  throws("formatEntry.nonMeta.optUndefinedThrows", function(){ B.formatEntry(e1); });

  // ---- strip option (empty-value fields) ----
  var e5=ent("@article{k5, a={x}, b={}}");
  var s5strip=B.formatEntry(e5,{strip:true});
  var s5keep=B.formatEntry(e5,{strip:false});
  ok("formatEntry.strip.removesEmptyValue", s5strip.indexOf("b =")===-1, s5strip);
  ok("formatEntry.strip.keepsEmptyWhenOff", s5keep.indexOf("b =")>=0, s5keep);
  ok("formatEntry.strip.emptyRenderedAsBraces", s5keep.indexOf("{}")>=0, s5keep);

  // ---- sortFields option ----
  var e6=ent("@article{k6, zebra={1}, alpha={2}}");
  var s6=B.formatEntry(e6,{sortFields:true});
  ok("formatEntry.sortFields.alphaBeforeZebra", s6.indexOf("alpha")<s6.indexOf("zebra"), s6);
  var s6n=B.formatEntry(e6,{sortFields:false});
  ok("formatEntry.sortFields.preservesOrderWhenOff", s6n.indexOf("zebra")<s6n.indexOf("alpha"), s6n);

  // ---- month resolution (no braces) ----
  var mo1=ent("@article{km, month={january}}");
  var smo1=B.formatEntry(mo1,O);
  eq("formatEntry.month.resolvedExact", smo1, "@article{km,\n  month = jan\n}");
  ok("formatEntry.month.resolvedNoBraces", smo1.indexOf("{jan}")===-1, smo1);
  var mo2=ent("@article{km2, month={3}, year={2021}}");
  ok("formatEntry.month.numericResolved", B.formatEntry(mo2,O).indexOf("month = mar,")>=0, B.formatEntry(mo2,O));
  var mo3=ent("@article{km3, month={foobar}}");
  ok("formatEntry.month.unknownKeepsBraces", B.formatEntry(mo3,O).indexOf("{foobar}")>=0, B.formatEntry(mo3,O));

  // ---- values containing braces preserved ----
  var eb=ent("@article{kb, title={The {DNA} Study}}");
  var sb=B.formatEntry(eb,O);
  ok("formatEntry.bracesInValue.preserved", sb.indexOf("{The {DNA} Study}")>=0, sb);

  // ---- unicode preserved ----
  var eu=ent("@article{ku, author={Müller Øystein 北京}}");
  ok("formatEntry.unicode.preserved", B.formatEntry(eu,O).indexOf("Müller Øystein 北京")>=0, B.formatEntry(eu,O));

  // ---- opt.lower on entry type ----
  var el=ent("@article{kl, a={b}}"); el.type="Article";
  ok("formatEntry.lower.true", B.formatEntry(el,{lower:true}).indexOf("@article{kl,")===0, B.formatEntry(el,{lower:true}));
  ok("formatEntry.lower.false", B.formatEntry(el,{lower:false}).indexOf("@Article{kl,")===0, B.formatEntry(el,{lower:false}));

  // ---- round trips ----
  var rt1=B.parseBib(B.formatEntry(e1,O)).entries[0];
  eqJSON("formatEntry.roundTrip.fieldsPreserved", rt1.fields, e1.fields);
  eq("formatEntry.roundTrip.keyPreserved", rt1.key, "key1");
  eq("formatEntry.roundTrip.typePreserved", rt1.type, "article");
  var rtm=B.parseBib(B.formatEntry(m1,O)).entries[0];
  eqJSON("formatEntry.roundTrip.metaPreserved", {t:rtm.type,r:rtm.raw,m:rtm.meta}, {t:"string",r:"acm = {ACM Press}",m:true});
  var rtb=B.parseBib(B.formatEntry(eb,O)).entries[0];
  eq("formatEntry.roundTrip.bracesValuePreserved", B.field(rtb,"title"), "The {DNA} Study");

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
