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
  var P=function(t){ return B.parseBib(t).entries[0]; };

  // ---- @article required: author/title/journal/year ----
  eq("validate.article.empty.count", B.validate(P("@article{k}")).length, 4);
  var ai=B.validate(P("@article{k}"));
  ok("validate.article.missing.author", ai.some(function(x){return /missing 'author'/.test(x.msg);}));
  ok("validate.article.missing.title", ai.some(function(x){return /missing 'title'/.test(x.msg);}));
  ok("validate.article.missing.journal", ai.some(function(x){return /missing 'journal'/.test(x.msg);}));
  ok("validate.article.missing.year", ai.some(function(x){return /missing 'year'/.test(x.msg);}));
  ok("validate.article.msg.shape", ai.some(function(x){return x.msg==="@article is missing 'author'";}));
  eq("validate.issue.level", ai[0].level, "w");
  eq("validate.issue.key", ai[0].key, "k");
  eqJSON("validate.article.valid.empty", B.validate(P("@article{k,author={A},title={T},journal={J},year={2020}}")), []);
  var aj=B.validate(P("@article{k,author={A},title={T},year={2020}}"));
  eq("validate.article.missingJournal.count", aj.length, 1);
  ok("validate.article.missingJournal.msg", aj.some(function(x){return /missing 'journal'/.test(x.msg);}));

  // ---- empty / whitespace / boundary values ----
  ok("validate.article.emptyValue.missing", B.validate(P("@article{k,author={},title={T},journal={J},year={2020}}")).some(function(x){return /missing 'author'/.test(x.msg);}));
  ok("validate.article.wsValue.missing", B.validate(P("@article{k,author={   },title={T},journal={J},year={2020}}")).some(function(x){return /missing 'author'/.test(x.msg);}));
  eqJSON("validate.article.yearZero.valid", B.validate(P("@article{k,author={A},title={T},journal={J},year={0}}")), []);

  // ---- @book: title/publisher/year + (author OR editor) ----
  var bk=B.validate(P("@book{k,title={T},publisher={P},year={2020}}"));
  ok("validate.book.needsAuthorOrEditor", bk.some(function(x){return /needs one of: author \/ editor/.test(x.msg);}));
  eq("validate.book.neither.count", bk.length, 1);
  eqJSON("validate.book.editorSatisfies", B.validate(P("@book{k,editor={E},title={T},publisher={P},year={2020}}")), []);
  eqJSON("validate.book.authorSatisfies", B.validate(P("@book{k,author={A},title={T},publisher={P},year={2020}}")), []);
  ok("validate.book.missingPublisher", B.validate(P("@book{k,author={A},title={T},year={2020}}")).some(function(x){return /missing 'publisher'/.test(x.msg);}));
  ok("validate.book.authorNotReq", !B.validate(P("@book{k,editor={E},title={T},publisher={P},year={2020}}")).some(function(x){return /missing 'author'/.test(x.msg);}));

  // ---- @inproceedings: author/title/booktitle/year ----
  var ip=B.validate(P("@inproceedings{k}"));
  eq("validate.inproc.empty.count", ip.length, 4);
  ok("validate.inproc.booktitle", ip.some(function(x){return /missing 'booktitle'/.test(x.msg);}));
  eqJSON("validate.inproc.valid", B.validate(P("@inproceedings{k,author={A},title={T},booktitle={B},year={2020}}")), []);

  // ---- @incollection: author/title/booktitle/publisher/year ----
  eq("validate.incoll.empty.count", B.validate(P("@incollection{k}")).length, 5);
  ok("validate.incoll.publisher", B.validate(P("@incollection{k}")).some(function(x){return /missing 'publisher'/.test(x.msg);}));

  // ---- @misc / @online: no required ----
  eqJSON("validate.misc.empty", B.validate(P("@misc{k}")), []);
  eqJSON("validate.misc.withFields", B.validate(P("@misc{k,title={T}}")), []);
  eqJSON("validate.online.empty", B.validate(P("@online{k}")), []);

  // ---- unknown types -> [] ----
  eqJSON("validate.unknownType", B.validate(P("@conference{k,foo={bar}}")), []);
  eqJSON("validate.unknownType2", B.validate(P("@blah{k}")), []);

  // ---- thesis / techreport / unpublished ----
  ok("validate.phd.school", B.validate(P("@phdthesis{k,author={A},title={T},year={2020}}")).some(function(x){return /missing 'school'/.test(x.msg);}));
  eqJSON("validate.phd.valid", B.validate(P("@phdthesis{k,author={A},title={T},school={S},year={2020}}")), []);
  ok("validate.masters.school", B.validate(P("@mastersthesis{k}")).some(function(x){return /missing 'school'/.test(x.msg);}));
  ok("validate.techreport.institution", B.validate(P("@techreport{k}")).some(function(x){return /missing 'institution'/.test(x.msg);}));
  ok("validate.unpub.note", B.validate(P("@unpublished{k,author={A},title={T}}")).some(function(x){return /missing 'note'/.test(x.msg);}));

  // ---- case-insensitivity (type + field name) ----
  ok("validate.typeCase.lower", B.validate(P("@ARTICLE{k}")).some(function(x){return x.msg==="@article is missing 'author'";}));
  eqJSON("validate.fieldNameCase", B.validate(P("@article{k,AUTHOR={A},TITLE={T},JOURNAL={J},YEAR={2020}}")), []);

  // ---- unicode values are present ----
  eqJSON("validate.unicode.valid", B.validate(P("@article{k,author={Ünäl},title={Tëst},journal={Jörnål},year={2021}}")), []);

  // ---- meta (@string) entry has no spec -> [] ----
  eqJSON("validate.stringMeta", B.validate(B.parseBib("@string{x={y}}").entries[0]), []);

  // ---- citation key propagation ----
  ok("validate.key.preserved", B.validate(P("@article{myKey}")).every(function(x){return x.key==="myKey";}));
  ok("validate.noKey.emptyKey", B.validate(P("@article{,author={A}}")).every(function(x){return x.key==="";}));

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
