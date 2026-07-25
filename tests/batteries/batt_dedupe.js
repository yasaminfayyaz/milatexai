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
  // helpers: D(text)=dedupe of parsed entries; F=field accessor; keys(list)=array of keys
  var F=B.field;
  function D(t){ return B.dedupe(B.parseBib(t).entries); }
  function keys(list){ return list.map(function(e){ return e.key; }); }

  // --- empties ---
  var e_empty=D("");
  eq("dedupe.empty.entriesLen", e_empty.entries.length, 0);
  eq("dedupe.empty.merged", e_empty.merged, 0);

  // --- single entry ---
  var e_one=D("@article{a, title={Solo}}");
  eq("dedupe.single.entriesLen", e_one.entries.length, 1);
  eq("dedupe.single.merged", e_one.merged, 0);

  // --- merge by identical key ---
  var e_key=D("@article{a, title={Some Long Title Here}, author={A}}\n@article{a, journal={JJ}}");
  eq("dedupe.byKey.entriesLen", e_key.entries.length, 1);
  eq("dedupe.byKey.merged", e_key.merged, 1);
  eq("dedupe.byKey.fillsBlankField", F(e_key.entries[0],"journal"), "JJ");
  eq("dedupe.byKey.keepsExistingTitle", F(e_key.entries[0],"title"), "Some Long Title Here");

  // conflict on a present field: first value kept
  var e_conf=D("@article{a, author={AA}}\n@article{a, author={BB}}");
  eq("dedupe.byKey.conflictKeepsFirst", F(e_conf.entries[0],"author"), "AA");
  eq("dedupe.byKey.conflict.merged", e_conf.merged, 1);

  // key match is case-insensitive
  var e_kc=D("@article{Foo, title={Xx}}\n@article{foo, journal={J}}");
  eq("dedupe.byKey.caseInsensitive.len", e_kc.entries.length, 1);
  eq("dedupe.byKey.caseInsensitive.fill", F(e_kc.entries[0],"journal"), "J");
  eq("dedupe.byKey.caseInsensitive.keepsFirstKey", e_kc.entries[0].key, "Foo");

  // chain of same-key entries merges all
  var e_chain=D("@article{a, f1={1}}\n@article{a, f2={2}}\n@article{a, f3={3}}");
  eq("dedupe.chainSameKey.len", e_chain.entries.length, 1);
  eq("dedupe.chainSameKey.merged", e_chain.merged, 2);
  eq("dedupe.chainSameKey.f1", F(e_chain.entries[0],"f1"), "1");
  eq("dedupe.chainSameKey.f2", F(e_chain.entries[0],"f2"), "2");
  eq("dedupe.chainSameKey.f3", F(e_chain.entries[0],"f3"), "3");

  // --- merge by DOI (case-insensitive) across different keys ---
  var e_doi=D("@article{a, doi={10.1/AbC}, title={Alpha}}\n@article{b, doi={10.1/abc}, journal={J}}");
  eq("dedupe.byDoi.len", e_doi.entries.length, 1);
  eq("dedupe.byDoi.merged", e_doi.merged, 1);
  eq("dedupe.byDoi.keepsFirstKey", e_doi.entries[0].key, "a");
  eq("dedupe.byDoi.fill", F(e_doi.entries[0],"journal"), "J");

  // DOI url / doi: prefixes normalized before compare
  var e_doiurl=D("@article{a, doi={https://doi.org/10.5/xyz}}\n@article{b, doi={doi:10.5/XYZ}}");
  eq("dedupe.byDoi.urlPrefixNormalized", e_doiurl.entries.length, 1);

  // same key but DIFFERENT doi still merges via the KEY branch (guard is title-only)
  var e_kd=D("@article{a, doi={10.1/x}, title={T Long Enough Here}}\n@article{a, doi={10.1/y}}");
  eq("dedupe.sameKeyDiffDoi.len", e_kd.entries.length, 1);
  eq("dedupe.sameKeyDiffDoi.keepsFirstDoi", F(e_kd.entries[0],"doi"), "10.1/x");

  // --- merge by identical normalized title (length>8) ---
  var e_tit=D("@article{a, title={The Quick Brown Fox}}\n@article{b, title={the  quick brown fox}}");
  eq("dedupe.byTitle.len", e_tit.entries.length, 1);
  eq("dedupe.byTitle.merged", e_tit.merged, 1);

  // short titles (<=8 chars normalized) are NOT title-merged
  var e_short=D("@article{a, title={Short}}\n@article{b, title={Short}}");
  eq("dedupe.byTitle.shortNotMerged", e_short.entries.length, 2);

  // boundary: normalized length exactly 8 -> not merged
  var e_b8=D("@article{a, title={abcdefgh}}\n@article{b, title={abcdefgh}}");
  eq("dedupe.byTitle.boundaryLen8NotMerged", e_b8.entries.length, 2);
  // boundary: normalized length 9 -> merged
  var e_b9=D("@article{a, title={abcdefghi}}\n@article{b, title={abcdefghi}}");
  eq("dedupe.byTitle.boundaryLen9Merged", e_b9.entries.length, 1);

  // --- conflicting-DOI guard: same title, different DOIs -> kept separate ---
  var e_guard=D("@article{a, title={A Very Long Title Here}, doi={10.1/one}}\n@article{b, title={A Very Long Title Here}, doi={10.1/two}}");
  eq("dedupe.conflictDoiGuard.keepsSeparate", e_guard.entries.length, 2);
  eq("dedupe.conflictDoiGuard.merged0", e_guard.merged, 0);

  // same title but only one has a DOI -> guard does not fire, they merge
  var e_onedoi=D("@article{a, title={A Very Long Title Here}, doi={10.1/one}}\n@article{b, title={A Very Long Title Here}}");
  eq("dedupe.titleMerge.oneDoiMissing", e_onedoi.entries.length, 1);
  eq("dedupe.titleMerge.oneDoiMissing.merged", e_onedoi.merged, 1);

  // --- no false merge on different titles ---
  var e_diff=D("@article{a, title={Completely Different One}}\n@article{b, title={Totally Unrelated Two}}");
  eq("dedupe.differentTitles.noMerge", e_diff.entries.length, 2);
  eq("dedupe.differentTitles.merged0", e_diff.merged, 0);

  // --- meta entries untouched and never merged ---
  var e_meta=D("@string{a = {b}}\n@article{k, title={Something Here Long}}\n@article{k, journal={J}}");
  eq("dedupe.meta.entriesLen", e_meta.entries.length, 2);
  eq("dedupe.meta.merged", e_meta.merged, 1);
  ok("dedupe.meta.firstIsMeta", e_meta.entries[0].meta===true, "expected meta flag");
  // two identical @string metas are both kept (metas never deduped)
  var e_meta2=D("@string{x = {y}}\n@string{x = {y}}");
  eq("dedupe.meta.identicalKeptBoth", e_meta2.entries.length, 2);

  // --- order preserved (merge in the middle) ---
  var e_ord=D("@article{x, title={First Long Title Alpha}}\n@article{y, title={Second Long Title Beta}}\n@article{x, note={n}}\n@article{z, title={Third Long Title Gamma}}");
  eqJSON("dedupe.orderPreserved.keys", keys(e_ord.entries), ["x","y","z"]);
  eq("dedupe.orderPreserved.merged", e_ord.merged, 1);

  // multiple independent merges counted
  var e_multi=D("@article{a, t={x}}\n@article{a, u={y}}\n@article{b, title={Long Enough Title Here}}\n@article{b, note={z}}");
  eq("dedupe.multiMerge.entriesLen", e_multi.entries.length, 2);
  eq("dedupe.multiMerge.merged", e_multi.merged, 2);

  // --- keyless, titleless, doiless entries never merge ---
  var e_bare=D("@article{, }\n@article{, }");
  eq("dedupe.bareEntries.notMerged", e_bare.entries.length, 2);
  eq("dedupe.bareEntries.merged0", e_bare.merged, 0);

  // --- unicode title: identical strings still merge ---
  var e_uni=D("@article{a, title={Über Ñoño Café Résumé}}\n@article{b, title={Über Ñoño Café Résumé}}");
  eq("dedupe.unicodeTitle.merges", e_uni.entries.length, 1);
  eq("dedupe.unicodeTitle.merged", e_uni.merged, 1);

  // --- return shape ---
  var e_shape=D("@article{a, title={Zzz}}");
  ok("dedupe.returnShape.entriesArray", Array.isArray(e_shape.entries), "entries not array");
  ok("dedupe.returnShape.mergedNumber", typeof e_shape.merged==="number", "merged not number");

  // --- adversarial inputs ---
  noThrow("dedupe.noThrow.emptyArray", function(){ B.dedupe([]); });
  throws("dedupe.throws.nullInput", function(){ B.dedupe(null); });
  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
