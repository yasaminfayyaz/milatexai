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

  function ent(bib){ return B.parseBib(bib).entries[0]; }

  /* ===== pickBestDoi : END-TO-END sync DOI selection ===== */

  // A researcher's entry for "Deep Residual Learning..." (He, 2016), no DOI yet.
  var eResid=ent("@article{r,title={Deep Residual Learning for Image Recognition},author={He, Kaiming},year={2016}}");
  var cCorrectResid={DOI:"10.1109/cvpr.2016.90",title:["Deep Residual Learning for Image Recognition"],author:[{family:"He"}],issued:{"date-parts":[[2016,6]]}};
  var cDistractorDL={DOI:"10.9999/wrong",title:["Deep Learning"],author:[{family:"He"}],issued:{"date-parts":[[2016]]}};
  var cDistractorSurvey={DOI:"10.8888/nope",title:["A Survey of Neural Networks"],author:[{family:"Smith"}],issued:{"date-parts":[[2011]]}};

  // 1. correct match wins among distractors (near-identical title -> jaccard rank3)
  eq("e2e_doi.exact_title_match_among_distractors",
     B.pickBestDoi(eResid,[cDistractorDL,cDistractorSurvey,cCorrectResid]),"10.1109/cvpr.2016.90");

  // 2. author+year match but title only 40% overlap must NOT be selected
  eq("e2e_doi.author_year_only_not_matched",B.pickBestDoi(eResid,[cDistractorDL]),null);

  // 3. near-duplicate WRONG paper (same author+year, 60% title overlap) rejected
  var eSurvey=ent("@article{s,title={A Survey of Machine Learning for Big Data Processing},author={Smith, John},year={2019}}");
  var cNearDup={DOI:"10.1234/neardup",title:["A Survey of Deep Learning for Big Image Processing"],author:[{family:"Smith"}],issued:{"date-parts":[[2019]]}};
  eq("e2e_doi.near_duplicate_wrong_paper_rejected",B.pickBestDoi(eSurvey,[cNearDup]),null);

  // 4. entry whose paper has NO Crossref record: all candidates unrelated -> null
  var eQuantum=ent("@article{q,title={Quantum Entanglement in Photosynthetic Complexes},author={Zhang, Wei},year={2021}}");
  var cUnrelated1={DOI:"10.1/a",title:["Machine Learning Basics"],author:[{family:"Brown"}],issued:{"date-parts":[[2005]]}};
  var cUnrelated2={DOI:"10.2/b",title:["Introduction to Relational Databases"],author:[{family:"Green"}],issued:{"date-parts":[[2000]]}};
  eq("e2e_doi.no_crossref_record_all_wrong",B.pickBestDoi(eQuantum,[cUnrelated1,cUnrelated2]),null);

  // 5. pure jaccard boundary: candidate has all entry words + 1 extra -> jac 6/7=0.857 -> rank3, no author/year needed
  var eGan=ent("@article{g,title={Generative Adversarial Networks Learning Realistic Images}}");
  var cGan={DOI:"10.5/gan",title:["Generative Adversarial Networks Learning Realistic Natural Images"]};
  eq("e2e_doi.jaccard_boundary_extra_word_rank3",B.pickBestDoi(eGan,[cGan]),"10.5/gan");

  // 6. long specific title fully contained, NO author/year: size>=5 branch -> rank2
  var eSpeech=ent("@article{sp,title={Deep Neural Networks for Speech Recognition}}");
  var cSpeech={DOI:"10.6/speech",title:["Deep Neural Networks for Large-Scale Speech Recognition Systems in Noisy Environments"]};
  eq("e2e_doi.long_title_contained_size5_no_author_year",B.pickBestDoi(eSpeech,[cSpeech]),"10.6/speech");

  // 7. size-4 title fully contained but NO author/year -> null (size<5 needs a/y)
  var eConv=ent("@article{c4,title={Convolutional Networks Visual Recognition}}");
  var cConv={DOI:"10.7/conv",title:["Convolutional Networks for Visual Recognition Tasks"]};
  eq("e2e_doi.size4_contained_no_author_year_null",B.pickBestDoi(eConv,[cConv]),null);

  // 8. same size-4 title fully contained WITH year match -> rank2
  var eConvY=ent("@article{c4y,title={Convolutional Networks Visual Recognition},year={2015}}");
  var cConvY={DOI:"10.8/conv",title:["Convolutional Networks for Visual Recognition Tasks"],issued:{"date-parts":[[2015]]}};
  eq("e2e_doi.size4_contained_with_year",B.pickBestDoi(eConvY,[cConvY]),"10.8/conv");

  // 9. title too thin (one long word) -> null even with a perfect candidate
  var ePhoto=ent("@article{p,title={Photosynthesis}}");
  eq("e2e_doi.title_too_short_size1_null",B.pickBestDoi(ePhoto,[{DOI:"10.9/x",title:["Photosynthesis"]}]),null);

  // 10. title made only of short words -> zero title words -> null
  var eShort=ent("@article{sh,title={The Big Red Cat Ran}}");
  eq("e2e_doi.title_all_short_words_null",B.pickBestDoi(eShort,[{DOI:"10.10/x",title:["The Big Red Cat Ran"]}]),null);

  // 11. unicode author match: "Müller" -> normalized "mller" matches candidate family "Müller"
  var eVar=ent("@article{v,title={Variational Methods Inverse Problems},author={Müller, Hans}}");
  var cVar={DOI:"10.11/var",title:["Variational Methods for Inverse Problems in Imaging"],author:[{family:"Müller"}]};
  eq("e2e_doi.unicode_author_muller_match",B.pickBestDoi(eVar,[cVar]),"10.11/var");

  // 12. same title, candidate author differs, no year -> null
  var cVarWrong={DOI:"10.12/var",title:["Variational Methods for Inverse Problems in Imaging"],author:[{family:"Schmidt"}]};
  eq("e2e_doi.author_mismatch_no_year_null",B.pickBestDoi(eVar,[cVarWrong]),null);

  // 13. year off by one still matches (abs diff <=1)
  var cConvY1={DOI:"10.13/conv",title:["Convolutional Networks for Visual Recognition Tasks"],issued:{"date-parts":[[2016]]}};
  eq("e2e_doi.year_off_by_one_matches",B.pickBestDoi(eConvY,[cConvY1]),"10.13/conv");

  // 14. year off by two, no author -> null
  var cConvY2={DOI:"10.14/conv",title:["Convolutional Networks for Visual Recognition Tasks"],issued:{"date-parts":[[2017]]}};
  eq("e2e_doi.year_off_by_two_null",B.pickBestDoi(eConvY,[cConvY2]),null);

  // 15. candidate missing DOI is skipped -> null
  eq("e2e_doi.candidate_missing_doi_skipped",
     B.pickBestDoi(eResid,[{title:["Deep Residual Learning for Image Recognition"],author:[{family:"He"}],issued:{"date-parts":[[2016]]}}]),null);

  // 16. candidate with empty title is skipped -> null
  eq("e2e_doi.candidate_empty_title_skipped",B.pickBestDoi(eResid,[{DOI:"10.16/x",title:[""]}]),null);

  // 17. empty items array -> null
  eq("e2e_doi.empty_items_null",B.pickBestDoi(eResid,[]),null);

  // 18. null items handled without throwing -> null
  noThrow("e2e_doi.null_items_no_throw",function(){ B.pickBestDoi(eResid,null); });
  eq("e2e_doi.null_items_null",B.pickBestDoi(eResid,null),null);

  // 19. contain exactly 0.9 (10-word title, candidate omits 1 word) -> rank2 via size>=5
  var eLong=ent("@article{l,title={Systematic Comparison Between Convolutional Recurrent Transformer Architectures Regarding Multilingual Translation}}");
  var cLong09={DOI:"10.19/long",title:["Systematic Comparison Between Convolutional Recurrent Transformer Architectures Multilingual Translation Study"]};
  eq("e2e_doi.contain_exactly_0_9_matches",B.pickBestDoi(eLong,[cLong09]),"10.19/long");

  // 20. contain 0.8 (omits 2 words) even with author+year -> below 0.9 -> null
  var eLong2=ent("@article{l2,title={Systematic Comparison Between Convolutional Recurrent Transformer Architectures Regarding Multilingual Translation},author={Lee, Ann},year={2020}}");
  var cLong08={DOI:"10.20/long",title:["Systematic Comparison Convolutional Recurrent Transformer Architectures Multilingual Translation Deep Study"],author:[{family:"Lee"}],issued:{"date-parts":[[2020]]}};
  eq("e2e_doi.contain_0_8_below_threshold_null",B.pickBestDoi(eLong2,[cLong08]),null);

  // 21. LaTeX markup in entry title still matches candidate (stripTex removes \emph, braces)
  var eTex=ent("@article{t,title={Deep \\emph{Residual} Learning for Image Recognition},author={He, Kaiming},year={2016}}");
  eq("e2e_doi.tex_in_title_still_matches",B.pickBestDoi(eTex,[cCorrectResid]),"10.1109/cvpr.2016.90");

  // 22/23. a rank3 (exact) candidate beats a rank2 (contained-but-noisy) one regardless of order
  var cResidNoisy={DOI:"10.22/noisy",title:["Deep Residual Learning for Image Recognition Revisited Extended Version Study Part"]};
  eq("e2e_doi.rank3_beats_rank2_order_noisy_first",
     B.pickBestDoi(eResid,[cResidNoisy,cCorrectResid]),"10.1109/cvpr.2016.90");
  eq("e2e_doi.rank3_beats_rank2_order_exact_first",
     B.pickBestDoi(eResid,[cCorrectResid,cResidNoisy]),"10.1109/cvpr.2016.90");

  // 24. first of two equal rank3 candidates wins (rank must strictly exceed to replace)
  var cResidExactA={DOI:"10.24/a",title:["Deep Residual Learning for Image Recognition"]};
  var cResidExactB={DOI:"10.24/b",title:["Deep Residual Learning for Image Recognition"]};
  eq("e2e_doi.first_of_equal_rank3_wins",B.pickBestDoi(eResid,[cResidExactA,cResidExactB]),"10.24/a");

  // 25. uppercase candidate family still matches (case-insensitive)
  var eConvA=ent("@article{ca,title={Convolutional Networks Visual Recognition},author={He, K}}");
  eq("e2e_doi.author_uppercase_family_matches",
     B.pickBestDoi(eConvA,[{DOI:"10.25/x",title:["Convolutional Networks for Visual Recognition Tasks"],author:[{family:"HE"}]}]),"10.25/x");

  // 26. passing a null entry throws (field() dereferences entry.fields)
  throws("e2e_doi.null_entry_throws",function(){ B.pickBestDoi(null,[]); });

  /* ===== bibtexFromDoiRecord : parse Crossref x-bibtex (fill-from-DOI step) ===== */

  var cross=[
    "@article{He_2016,",
    " series = {CVPR '16},",
    " doi = {10.1109/cvpr.2016.90},",
    " url = {https://doi.org/10.1109/cvpr.2016.90},",
    " year = {2016},",
    " month = jun,",
    " publisher = {IEEE},",
    " volume = {1},",
    " number = {2},",
    " pages = {770--778},",
    " title = {Deep Residual Learning for Image Recognition},",
    " author = {He, Kaiming and Zhang, Xiangyu and Ren, Shaoqing and Sun, Jian},",
    " journal = {2016 IEEE Conference on Computer Vision and Pattern Recognition}",
    "}"
  ].join("\n");
  var rec1=B.bibtexFromDoiRecord(cross);

  eq("e2e_doi.rec_type",rec1.type,"article");
  eq("e2e_doi.rec_key",rec1.key,"He_2016");
  eq("e2e_doi.rec_title",B.field(rec1,"title"),"Deep Residual Learning for Image Recognition");
  eq("e2e_doi.rec_author",B.field(rec1,"author"),"He, Kaiming and Zhang, Xiangyu and Ren, Shaoqing and Sun, Jian");
  eq("e2e_doi.rec_year",B.field(rec1,"year"),"2016");
  eq("e2e_doi.rec_doi",B.field(rec1,"doi"),"10.1109/cvpr.2016.90");
  eq("e2e_doi.rec_month_bare",B.field(rec1,"month"),"jun");
  eq("e2e_doi.rec_publisher",B.field(rec1,"publisher"),"IEEE");
  eq("e2e_doi.rec_volume",B.field(rec1,"volume"),"1");
  eq("e2e_doi.rec_number",B.field(rec1,"number"),"2");
  eq("e2e_doi.rec_pages",B.field(rec1,"pages"),"770--778");
  eq("e2e_doi.rec_journal",B.field(rec1,"journal"),"2016 IEEE Conference on Computer Vision and Pattern Recognition");
  eq("e2e_doi.rec_missing_field_empty",B.field(rec1,"booktitle"),"");

  // nested braces / TeX unicode accents preserved verbatim
  var recU=B.bibtexFromDoiRecord('@article{k,title={Sch{\\"o}lkopf Kernel Methods},year={2002}}');
  eq("e2e_doi.rec_nested_braces_unicode",B.field(recU,"title"),'Sch{\\"o}lkopf Kernel Methods');

  // literal (non-ASCII) unicode preserved
  var recL=B.bibtexFromDoiRecord("@article{k2,title={Café Studies of Naïve Bayes},year={2010}}");
  eq("e2e_doi.rec_literal_unicode",B.field(recL,"title"),"Café Studies of Naïve Bayes");

  // inproceedings type preserved
  var recIP=B.bibtexFromDoiRecord("@inproceedings{Foo_2020,doi={10.1/x},title={A Deep Study of Systems},year={2020}}");
  eq("e2e_doi.rec_inproceedings_type",recIP.type,"inproceedings");

  // empty string -> no entry -> null
  eq("e2e_doi.rec_empty_string_null",B.bibtexFromDoiRecord(""),null);
  // plain non-bibtex text -> null
  eq("e2e_doi.rec_plain_text_null",B.bibtexFromDoiRecord("this is not bibtex at all"),null);
  // @string-only (meta) -> null (skips meta entries)
  eq("e2e_doi.rec_string_only_null",B.bibtexFromDoiRecord("@string{ieee = {IEEE Press}}"),null);
  // returns FIRST non-meta entry when preceded by an @string macro
  var recFirst=B.bibtexFromDoiRecord("@string{x={Y}}\n@article{first,title={Real Deep Learning Paper},year={2021}}");
  eq("e2e_doi.rec_returns_first_nonmeta",recFirst.key,"first");
  // null input throws (parseBib reads .length)
  throws("e2e_doi.rec_null_throws",function(){ B.bibtexFromDoiRecord(null); });

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
