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

  // ---- normTitleKey: lowercasing + stripping ----
  eq("helpers.normTitleKey_basic_lower", B.normTitleKey("Hello World"), "hello world");
  eq("helpers.normTitleKey_strip_braces", B.normTitleKey("The {LaTeX} Guide"), "the latex guide");
  eq("helpers.normTitleKey_strip_backslash", B.normTitleKey("\\alpha Beta"), "alpha beta");
  eq("helpers.normTitleKey_strip_dollar", B.normTitleKey("Cost $5 each"), "cost 5 each");
  eq("helpers.normTitleKey_punct_to_space", B.normTitleKey("A.B,C!"), "a b c");
  eq("helpers.normTitleKey_trim", B.normTitleKey("   spaced   "), "spaced");
  eq("helpers.normTitleKey_collapse_symbols", B.normTitleKey("a---b___c"), "a b c");
  eq("helpers.normTitleKey_numbers_kept", B.normTitleKey("Test123 456"), "test123 456");
  eq("helpers.normTitleKey_only_symbols", B.normTitleKey("$$${}\\"), "");
  eq("helpers.normTitleKey_null", B.normTitleKey(null), "");
  eq("helpers.normTitleKey_undefined", B.normTitleKey(undefined), "");
  eq("helpers.normTitleKey_empty", B.normTitleKey(""), "");
  eq("helpers.normTitleKey_unicode_trailing_drop", B.normTitleKey("Café"), "cafe");
  eq("helpers.normTitleKey_unicode_accented_char_drop", B.normTitleKey("Café"), "caf");
  eq("helpers.normTitleKey_unicode_mid_split", B.normTitleKey("naïve"), "na ve");
  eq("helpers.normTitleKey_leading_symbols", B.normTitleKey("!!!Wow"), "wow");
  throws("helpers.normTitleKey_number_input_throws", function(){ B.normTitleKey(123); });

  // ---- _titleWords: length>3 filter + dedupe ----
  eqJSON("helpers.titleWords_len_filter", arr(B._titleWords("the quick brown fox")), ["brown","quick"]);
  eqJSON("helpers.titleWords_dedupe_casefolded", arr(B._titleWords("test Test TEST")), ["test"]);
  eqJSON("helpers.titleWords_exact4_kept", arr(B._titleWords("abcd xyz")), ["abcd"]);
  eqJSON("helpers.titleWords_len3_all_dropped", arr(B._titleWords("cat dog pig")), []);
  eqJSON("helpers.titleWords_empty", arr(B._titleWords("")), []);
  eqJSON("helpers.titleWords_unicode_short_dropped", arr(B._titleWords("Café")), []);
  eqJSON("helpers.titleWords_multiword_sorted", arr(B._titleWords("Learning Deep Neural Networks")), ["deep","learning","networks","neural"]);
  ok("helpers.titleWords_size_three", B._titleWords("apple banana cherry").size===3, "size not 3");
  ok("helpers.titleWords_returns_set", B._titleWords("apple banana") instanceof Set, "not a Set");

  // ---- _jaccard: intersection over union ----
  eq("helpers.jaccard_identical_one", B._jaccard(B._titleWords("apple banana"), B._titleWords("apple banana")), 1);
  eq("helpers.jaccard_disjoint_zero", B._jaccard(B._titleWords("apple banana"), B._titleWords("cherry mango")), 0);
  eq("helpers.jaccard_one_third", B._jaccard(B._titleWords("apple banana"), B._titleWords("banana cherry")), 1/3);
  eq("helpers.jaccard_half", B._jaccard(B._titleWords("apple banana cherry"), B._titleWords("banana cherry mango")), 0.5);
  eq("helpers.jaccard_emptyA_zero", B._jaccard(B._titleWords(""), B._titleWords("apple banana")), 0);
  eq("helpers.jaccard_emptyB_zero", B._jaccard(B._titleWords("apple banana"), B._titleWords("")), 0);
  eq("helpers.jaccard_both_empty_zero", B._jaccard(B._titleWords(""), B._titleWords("")), 0);
  ok("helpers.jaccard_in_range", (function(){ var v=B._jaccard(B._titleWords("apple banana cherry"), B._titleWords("banana cherry mango")); return v>=0 && v<=1; })(), "out of [0,1]");
  noThrow("helpers.jaccard_self_multiword_nothrow", function(){ B._jaccard(B._titleWords("alpha beta gamma"), B._titleWords("alpha beta gamma")); });

  // ---- integration via parseBib ----
  eq("helpers.integration_parseBib_normTitleKey", B.normTitleKey(B.field(B.parseBib("@article{k, title={The {LaTeX} Guide}}").entries[0],"title")), "the latex guide");
  eqJSON("helpers.integration_parseBib_titleWords", arr(B._titleWords(B.field(B.parseBib("@article{k, title={Deep Neural Networks}}").entries[0],"title"))), ["deep","networks","neural"]);

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
