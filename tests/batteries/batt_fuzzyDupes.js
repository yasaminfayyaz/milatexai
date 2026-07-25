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
  function P(s){ return B.parseBib(s).entries; }

  // ---- near-identical titles (different author/year) ----
  var aC=B.fuzzyDupes(P("@article{a1, title={Deep Learning for Natural Language Processing}, author={Alice Brown}, year={2019}}\n@article{a2, title={Deep Learning for Natural Language Processing}, author={Charlie Green}, year={2021}}"));
  eq("fuzzyDupes.nearIdentical.length", aC.length, 1);
  eqJSON("fuzzyDupes.nearIdentical.shape", aC, [{a:"a1",b:"a2",reason:"near-identical titles",sim:100}]);
  eq("fuzzyDupes.nearIdentical.reason", aC[0].reason, "near-identical titles");
  eq("fuzzyDupes.nearIdentical.sim100", aC[0].sim, 100);

  // ---- one title with an added subtitle (same author + year) ----
  var bC=B.fuzzyDupes(P("@article{b1, title={Attention Is All You Need}, author={Vaswani, Ashish}, year={2017}}\n@article{b2, title={Attention Is All You Need: A Transformer Architecture}, author={Vaswani, Ashish}, year={2017}}"));
  eq("fuzzyDupes.subtitle.length", bC.length, 1);
  eqJSON("fuzzyDupes.subtitle.shape", bC, [{a:"b1",b:"b2",reason:"same first author + year, similar title",sim:50}]);
  eq("fuzzyDupes.subtitle.reason", bC[0].reason, "same first author + year, similar title");
  eq("fuzzyDupes.subtitle.sim50", bC[0].sim, 50);

  // ---- same first author + year + partial title overlap ----
  var cC=B.fuzzyDupes(P("@article{c1, title={A Study of Graph Neural Networks}, author={Smith, John}, year={2020}}\n@article{c2, title={Graph Neural Networks for Recommendation}, author={Smith, Jane}, year={2020}}"));
  eq("fuzzyDupes.sameAuthorYear.length", cC.length, 1);
  eq("fuzzyDupes.sameAuthorYear.reason", cC[0].reason, "same first author + year, similar title");
  eq("fuzzyDupes.sameAuthorYear.sim60", cC[0].sim, 60);

  // ---- both entries carry DOIs => skipped ----
  var dC=B.fuzzyDupes(P("@article{d1, title={Deep Learning for Natural Language Processing}, doi={10.1/abc}, author={Alice Brown}, year={2019}}\n@article{d2, title={Deep Learning for Natural Language Processing}, doi={10.2/xyz}, author={Alice Brown}, year={2019}}"));
  eq("fuzzyDupes.bothDoi.skipped", dC.length, 0);

  // ---- only one has a DOI => still flagged ----
  var eC=B.fuzzyDupes(P("@article{e1, title={Deep Learning for Natural Language Processing}, doi={10.1/abc}}\n@article{e2, title={Deep Learning for Natural Language Processing}}"));
  eq("fuzzyDupes.oneDoi.length", eC.length, 1);
  eq("fuzzyDupes.oneDoi.sim100", eC[0].sim, 100);

  // ---- both DOIs but differently formatted => still skipped ----
  var tC=B.fuzzyDupes(P("@article{t1, title={Deep Learning for Vision}, doi={https://doi.org/10.1/AB}}\n@article{t2, title={Deep Learning for Vision}, doi={10.2/cd}}"));
  eq("fuzzyDupes.bothDoiFormats.skipped", tC.length, 0);

  // ---- empty doi field value is not a real DOI => flagged ----
  var vC=B.fuzzyDupes(P("@article{v1, title={Deep Learning for Vision}, doi={}}\n@article{v2, title={Deep Learning for Vision}}"));
  eq("fuzzyDupes.emptyDoiField.flagged", vC.length, 1);

  // ---- genuinely different references => no flags ----
  var fC=B.fuzzyDupes(P("@article{f1, title={Quantum Computing Algorithms}, author={Alice Brown}, year={2019}}\n@article{f2, title={Culinary Traditions of Southern Italy}, author={Bob White}, year={2005}}"));
  eq("fuzzyDupes.different.noFlags", fC.length, 0);

  // ---- boundary: sim 0.6 with different author/year (below 0.8, no sameAY) ----
  var kC=B.fuzzyDupes(P("@article{k1, title={Alpha Beta Gamma Delta}, author={Brown, Alice}, year={2019}}\n@article{k2, title={Alpha Beta Gamma Epsilon}, author={White, Bob}, year={2020}}"));
  eq("fuzzyDupes.below80NoSameAY.noFlag", kC.length, 0);

  // ---- boundary: sim exactly 0.8 => near-identical even with different authors ----
  var mC=B.fuzzyDupes(P("@article{m1, title={Alpha Beta Gamma Delta Omega}, author={Brown, Alice}, year={2001}}\n@article{m2, title={Alpha Beta Gamma Delta}, author={White, Bob}, year={2002}}"));
  eq("fuzzyDupes.sim80.length", mC.length, 1);
  eq("fuzzyDupes.sim80.reason", mC[0].reason, "near-identical titles");
  eq("fuzzyDupes.sim80.sim", mC[0].sim, 80);

  // ---- boundary: sim 0.4 with sameAY (below 0.5) => no flag ----
  var pC=B.fuzzyDupes(P("@article{p1, title={Alpha Beta Gamma}, author={Lee, Kim}, year={2011}}\n@article{p2, title={Alpha Beta Delta Omega}, author={Lee, Sam}, year={2011}}"));
  eq("fuzzyDupes.sameAYbelow50.noFlag", pC.length, 0);

  // ---- author format variants (First Last vs Last, First) + multiple authors ----
  var qC=B.fuzzyDupes(P("@article{q1, title={Neural Machine Translation Systems}, author={Yoshua Bengio and Ian Goodfellow}, year={2016}}\n@article{q2, title={Neural Machine Translation Models}, author={Bengio, Yoshua and Aaron Courville}, year={2016}}"));
  eq("fuzzyDupes.authorFormatVariants.length", qC.length, 1);
  eq("fuzzyDupes.authorFormatVariants.reason", qC[0].reason, "same first author + year, similar title");
  eq("fuzzyDupes.authorFormatVariants.sim60", qC[0].sim, 60);

  // ---- year normalization: trailing text stripped, first 4 digits used ----
  var rC=B.fuzzyDupes(P("@article{r1, title={Reinforcement Learning Foundations}, author={Sutton, Richard}, year={2020}}\n@article{r2, title={Reinforcement Learning Principles}, author={Sutton, Barto}, year={2020 (preprint)}}"));
  eq("fuzzyDupes.yearNormalize.length", rC.length, 1);
  eq("fuzzyDupes.yearNormalize.sim50", rC[0].sim, 50);

  // ---- three identical titles => three pairs ----
  var sC=B.fuzzyDupes(P("@article{s1, title={Deep Learning for Vision}, author={A B}, year={2001}}\n@article{s2, title={Deep Learning for Vision}, author={C D}, year={2002}}\n@article{s3, title={Deep Learning for Vision}, author={E F}, year={2003}}"));
  eq("fuzzyDupes.threePairs.length", sC.length, 3);
  eqJSON("fuzzyDupes.threePairs.keys", sC.map(function(p){return p.a+"|"+p.b;}).sort(), ["s1|s2","s1|s3","s2|s3"]);

  // ---- empty input ----
  eq("fuzzyDupes.empty.length", B.fuzzyDupes([]).length, 0);

  // ---- single entry: no pairs ----
  eq("fuzzyDupes.single.length", B.fuzzyDupes(P("@article{z1, title={Deep Learning for Vision}}")).length, 0);

  // ---- meta entries (@string) are filtered out before pairing ----
  var gC=B.fuzzyDupes(P("@string{jphys = {Journal of Physics}}\n@article{g1, title={Deep Learning for Vision}}\n@article{g2, title={Deep Learning for Vision}}"));
  eq("fuzzyDupes.metaFiltered.length", gC.length, 1);
  eq("fuzzyDupes.metaFiltered.reason", gC[0].reason, "near-identical titles");

  // ---- only meta entries => nothing to compare ----
  eq("fuzzyDupes.metaOnly.length", B.fuzzyDupes(P("@string{a = {Alpha}}\n@string{b = {Beta}}")).length, 0);

  // ---- entries with no title never flagged (empty word sets) ----
  var hC=B.fuzzyDupes(P("@article{h1, author={Smith, John}, year={2020}}\n@article{h2, author={Smith, John}, year={2020}}"));
  eq("fuzzyDupes.noTitle.noFlag", hC.length, 0);

  // ---- missing citation keys reported as "(no key)" ----
  var noKey=B.fuzzyDupes(P("@article{, title={Deep Learning for Natural Language Processing}}\n@article{, title={Deep Learning for Natural Language Processing}}"));
  eq("fuzzyDupes.missingKey.length", noKey.length, 1);
  eq("fuzzyDupes.missingKey.aLabel", noKey[0].a, "(no key)");
  eq("fuzzyDupes.missingKey.bLabel", noKey[0].b, "(no key)");

  // ---- unicode titles (accents stripped by normalization, still matched) ----
  var uC=B.fuzzyDupes(P("@article{u1, title={Étude sur les Réseaux de Neurones}, author={Müller, Hans}, year={2018}}\n@article{u2, title={Étude sur les Réseaux de Neurones}, author={Dupont, Marie}, year={2019}}"));
  eq("fuzzyDupes.unicode.length", uC.length, 1);
  eq("fuzzyDupes.unicode.reason", uC[0].reason, "near-identical titles");
  eq("fuzzyDupes.unicode.sim100", uC[0].sim, 100);

  // ---- adversarial: null / undefined throw ----
  throws("fuzzyDupes.nullThrows", function(){ B.fuzzyDupes(null); });
  throws("fuzzyDupes.undefinedThrows", function(){ B.fuzzyDupes(undefined); });

  // ---- adversarial: bare objects without fields do not throw, no flags ----
  noThrow("fuzzyDupes.bareObjects.noThrow", function(){ B.fuzzyDupes([{key:"x"},{key:"y"}]); });
  eq("fuzzyDupes.bareObjects.length", B.fuzzyDupes([{key:"x"},{key:"y"}]).length, 0);

  // ---- adversarial: meta:true objects filtered out ----
  eq("fuzzyDupes.metaTrueObjects.length", B.fuzzyDupes([{meta:true,key:"m1"},{meta:true,key:"m2"}]).length, 0);

  // ---- return value is always an array ----
  ok("fuzzyDupes.returnsArray", Array.isArray(B.fuzzyDupes(P("@article{w1, title={X Y}}"))), "not array");

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
