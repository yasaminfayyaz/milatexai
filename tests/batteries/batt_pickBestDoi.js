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
  function mk(bib){ return B.parseBib(bib).entries[0]; }
  // Entries used across cases
  var E5   = mk("@a{k, title={Deep Learning for Image Recognition Systems}, author={Smith, John}, year={2020}}"); // words={deep,learning,image,recognition,systems} sz5 smith 2020
  var E3   = mk("@a{k, title={Attention Mechanisms in Transformers}, author={Vaswani, Ashish}, year={2017}}");    // sz3 vaswani 2017
  var E3n  = mk("@a{k, title={Attention Mechanisms in Transformers}}");                                            // sz3 no author/year
  var E4   = mk("@a{k, title={Generative Adversarial Networks Framework}, author={Goodfellow, Ian}, year={2020}}");// sz4 goodfellow 2020
  var E6   = mk("@a{k, title={Deep Convolutional Networks Object Detection Benchmark}}");                          // sz6 no author/year
  var E1   = mk("@a{k, title={Networks}}");                                                                        // sz1
  var Esh  = mk("@a{k, title={AI ML}}");                                                                           // sz0 (short words)
  var Ent  = mk("@a{k, author={Smith, John}, year={2020}}");                                                       // no title
  var Euni = mk("@a{k, title={Étude sur les Réseaux Neuronaux}}");                                                 // unicode
  var Epun = mk("@a{k, title={Machine-Learning: A Review (2020)!}, author={Ng, Andrew}, year={2020}}");            // sz4 ng 2020

  var exact5 = "Deep Learning for Image Recognition Systems";

  // --- exact / near title accepted ---
  eq("pickBestDoi.exactTitleFullMeta", B.pickBestDoi(E5,[{DOI:"10.1/exact", title:[exact5], author:[{family:"Smith"}], issued:{"date-parts":[[2020]]}}]), "10.1/exact");
  eq("pickBestDoi.exactTitleNoAuthorYear", B.pickBestDoi(E5,[{DOI:"10.1/b", title:[exact5]}]), "10.1/b"); // jac=1 -> rank3 alone
  eq("pickBestDoi.nearTitleExtraWord_size5_rank2", B.pickBestDoi(E5,[{DOI:"10.1/near", title:["Deep Learning for Image Recognition Systems Advanced"]}]), "10.1/near"); // contain=1,sz5 -> rank2
  eq("pickBestDoi.nearJaccard_6of7_rank3", B.pickBestDoi(E6,[{DOI:"10.1/jac", title:["Deep Convolutional Networks Object Detection Benchmark Study"]}]), "10.1/jac"); // jac=6/7=0.857>=0.85

  // --- clearly different / insufficient overlap -> null ---
  eq("pickBestDoi.clearlyDifferent", B.pickBestDoi(E5,[{DOI:"10.1/c", title:["Quantum Cryptography Protocols Overview"], author:[{family:"Smith"}], issued:{"date-parts":[[2020]]}}]), null);
  eq("pickBestDoi.partialOverlap_authorYearMatch_stillNull", B.pickBestDoi(E5,[{DOI:"10.1/p", title:["Deep Learning Image Classification Approach"], author:[{family:"Smith"}], issued:{"date-parts":[[2020]]}}]), null); // contain=0.6 -> null even w/ author+year

  // --- empties ---
  eq("pickBestDoi.emptyItems", B.pickBestDoi(E5,[]), null);
  eq("pickBestDoi.nullItems", B.pickBestDoi(E5,null), null);
  eq("pickBestDoi.undefinedItems", B.pickBestDoi(E5), null);
  noThrow("pickBestDoi.emptyItemsNoThrow", function(){ B.pickBestDoi(E5,[]); });

  // --- ranking among several candidates ---
  eq("pickBestDoi.rank2ThenRank3_picksRank3", B.pickBestDoi(E5,[
    {DOI:"10.1/e2", title:["Deep Learning for Image Recognition Systems Advanced"]},
    {DOI:"10.1/e3", title:[exact5]}
  ]), "10.1/e3");
  eq("pickBestDoi.rank3ThenRank2_keepsRank3", B.pickBestDoi(E5,[
    {DOI:"10.1/e3", title:[exact5]},
    {DOI:"10.1/e2", title:["Deep Learning for Image Recognition Systems Advanced"]}
  ]), "10.1/e3");
  eq("pickBestDoi.tieRank3_keepsFirst", B.pickBestDoi(E5,[
    {DOI:"10.1/first", title:[exact5]},
    {DOI:"10.1/second", title:[exact5]}
  ]), "10.1/first");
  eq("pickBestDoi.tieRank2_keepsFirst", B.pickBestDoi(E5,[
    {DOI:"10.1/r2a", title:["Deep Learning for Image Recognition Systems Advanced"]},
    {DOI:"10.1/r2b", title:["Deep Learning for Image Recognition Systems Extended"]}
  ]), "10.1/r2a");

  // --- subtitle candidate + author + year -> accept (short title needs both) ---
  eq("pickBestDoi.subtitle_author_year_size3_rank3", B.pickBestDoi(E3,[{DOI:"10.1/sub", title:["Attention Mechanisms in Transformers: A Comprehensive Study"], author:[{family:"Vaswani"}], issued:{"date-parts":[[2017]]}}]), "10.1/sub");

  // --- subset title with NO author/year -> null ---
  eq("pickBestDoi.subsetNoAuthorNoYear_size3_null", B.pickBestDoi(E3n,[{DOI:"10.1/g", title:["Attention Mechanisms in Transformers: A Comprehensive Study"]}]), null);

  // --- author matches but year decades off -> null (short title) ---
  eq("pickBestDoi.authorMatchYearDecadesOff_size3_null", B.pickBestDoi(E3,[{DOI:"10.1/h", title:["Attention Mechanisms in Transformers: A Study"], author:[{family:"Vaswani"}], issued:{"date-parts":[[1980]]}}]), null);

  // --- size>=4 contain path: author OR year alone suffices ---
  eq("pickBestDoi.size4_authorOnly_rank2", B.pickBestDoi(E4,[{DOI:"10.1/i", title:["Generative Adversarial Networks Framework Extended"], author:[{family:"Goodfellow"}], issued:{"date-parts":[[1980]]}}]), "10.1/i");
  eq("pickBestDoi.size4_yearOnly_rank2", B.pickBestDoi(E4,[{DOI:"10.1/j", title:["Generative Adversarial Networks Framework Extended"], author:[{family:"Other"}], issued:{"date-parts":[[2020]]}}]), "10.1/j");
  eq("pickBestDoi.size4_yearOffBy1_rank2", B.pickBestDoi(E4,[{DOI:"10.1/y1", title:["Generative Adversarial Networks Framework Extended"], author:[{family:"Other"}], issued:{"date-parts":[[2021]]}}]), "10.1/y1"); // |2020-2021|=1 -> yMatch
  eq("pickBestDoi.size4_yearOffBy2_noAuthor_null", B.pickBestDoi(E4,[{DOI:"10.1/y2", title:["Generative Adversarial Networks Framework Extended"], author:[{family:"Other"}], issued:{"date-parts":[[2022]]}}]), null); // |2020-2022|=2 -> null
  eq("pickBestDoi.size4_noAuthorNoYear_null", B.pickBestDoi(E4,[{DOI:"10.1/n", title:["Generative Adversarial Networks Framework Extended"]}]), null); // contain=1 sz4 but no a/y -> null

  // --- one-word / too-short entry titles -> null ---
  eq("pickBestDoi.oneWordTitle_null", B.pickBestDoi(E1,[{DOI:"10.1/x", title:["Networks"]}]), null);
  eq("pickBestDoi.shortWordsTitle_null", B.pickBestDoi(Esh,[{DOI:"10.1/x", title:["AI ML"]}]), null);
  eq("pickBestDoi.noTitleEntry_null", B.pickBestDoi(Ent,[{DOI:"10.1/x", title:[exact5]}]), null);

  // --- malformed / missing candidate fields: skip gracefully, no crash ---
  eq("pickBestDoi.candidateMissingDOI_skipped", B.pickBestDoi(E5,[{title:[exact5], author:[{family:"Smith"}]}]), null);
  eq("pickBestDoi.candidateMissingTitle_skipped", B.pickBestDoi(E5,[{DOI:"10.1/x"}]), null);
  eq("pickBestDoi.candidateEmptyTitleArray_skipped", B.pickBestDoi(E5,[{DOI:"10.1/x", title:[]}]), null);
  eq("pickBestDoi.candidateMissingIssued_size4_authorMatch", B.pickBestDoi(E4,[{DOI:"10.1/q", title:["Generative Adversarial Networks Framework Extended"], author:[{family:"Goodfellow"}]}]), "10.1/q");
  eq("pickBestDoi.candidateMissingAuthor_exactStillAccepts", B.pickBestDoi(E5,[{DOI:"10.1/ma", title:[exact5]}]), "10.1/ma");
  eq("pickBestDoi.candidateWeirdAuthorEntries_noCrash", B.pickBestDoi(E5,[{DOI:"10.1/w", title:[exact5], author:[{},{family:null}]}]), "10.1/w");
  eq("pickBestDoi.nullElementInItems_skipped", B.pickBestDoi(E5,[null,{DOI:"10.1/ok", title:[exact5]}]), "10.1/ok");
  noThrow("pickBestDoi.malformedCandidatesNoThrow", function(){ B.pickBestDoi(E5,[null,{},{DOI:"z"},{DOI:"z",title:[]},{DOI:"z",title:[null]}]); });
  noThrow("pickBestDoi.candidateAuthorNotArrayNoThrow", function(){ B.pickBestDoi(E5,[{DOI:"z", title:[exact5]}]); });

  // --- unicode & punctuation titles ---
  eq("pickBestDoi.unicodeIdenticalTitle_accept", B.pickBestDoi(Euni,[{DOI:"10.1/u", title:["Étude sur les Réseaux Neuronaux"]}]), "10.1/u");
  eq("pickBestDoi.punctuationNormalizedTitle_accept", B.pickBestDoi(Epun,[{DOI:"10.1/pu", title:["Machine Learning, A Review 2020"], author:[{family:"Ng"}], issued:{"date-parts":[[2020]]}}]), "10.1/pu");
  noThrow("pickBestDoi.unicodeNoThrow", function(){ B.pickBestDoi(Euni,[{DOI:"z", title:["Étude sur les Réseaux Neuronaux"]}]); });

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
