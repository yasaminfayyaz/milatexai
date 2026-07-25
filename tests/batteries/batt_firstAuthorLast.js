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

  // ---- comma style "Last, First and ..." ----
  eq("firstAuthorLast.commaStyleFirstAuthor", B.firstAuthorLast("Smith, John and Doe, Jane"), "smith");
  eq("firstAuthorLast.commaStyleSingle", B.firstAuthorLast("Smith, John"), "smith");
  eq("firstAuthorLast.commaTrailingOnly", B.firstAuthorLast("Smith,"), "smith");
  eq("firstAuthorLast.commaLeadingEmptyLast", B.firstAuthorLast(", John"), "");
  eq("firstAuthorLast.commaWithInitialDot", B.firstAuthorLast("Smith, J."), "smith");

  // ---- natural style "First Last and ..." ----
  eq("firstAuthorLast.naturalStyleFirstAuthor", B.firstAuthorLast("John Smith and Jane Doe"), "smith");
  eq("firstAuthorLast.naturalSingle", B.firstAuthorLast("John Smith"), "smith");
  eq("firstAuthorLast.naturalInitials", B.firstAuthorLast("J. R. R. Tolkien"), "tolkien");

  // ---- single token ----
  eq("firstAuthorLast.singleToken", B.firstAuthorLast("Smith"), "smith");
  eq("firstAuthorLast.singleTokenUpper", B.firstAuthorLast("SMITH"), "smith");

  // ---- braces, dots, backslash stripped ----
  eq("firstAuthorLast.bracesStripped", B.firstAuthorLast("{Smith}"), "smith");
  eq("firstAuthorLast.bracesSplitNatural", B.firstAuthorLast("{Smith Jones}"), "jones");
  eq("firstAuthorLast.dotsBackslashBraces", B.firstAuthorLast("a.b\\c{d}e"), "abcde");

  // ---- hyphenated last names (hyphen preserved) ----
  eq("firstAuthorLast.hyphenNatural", B.firstAuthorLast("Jane Smith-Jones"), "smith-jones");
  eq("firstAuthorLast.hyphenComma", B.firstAuthorLast("Sartre-Beauvoir, Jean"), "sartre-beauvoir");
  eq("firstAuthorLast.hyphenFirstNameNatural", B.firstAuthorLast("Jean-Paul Sartre"), "sartre");

  // ---- particles / von (actual behavior differs by style) ----
  eq("firstAuthorLast.vonComma", B.firstAuthorLast("von Neumann, John"), "von neumann");
  eq("firstAuthorLast.vonNatural", B.firstAuthorLast("John von Neumann"), "neumann");

  // ---- empty / undefined / null / falsy ----
  eq("firstAuthorLast.emptyString", B.firstAuthorLast(""), "");
  eq("firstAuthorLast.undefinedInput", B.firstAuthorLast(undefined), "");
  eq("firstAuthorLast.nullInput", B.firstAuthorLast(null), "");
  eq("firstAuthorLast.zeroFalsy", B.firstAuthorLast(0), "");
  eq("firstAuthorLast.whitespaceOnly", B.firstAuthorLast("   "), "");

  // ---- unicode names ----
  eq("firstAuthorLast.unicodeNatural", B.firstAuthorLast("Carl Gauß"), "gauß");
  eq("firstAuthorLast.unicodeComma", B.firstAuthorLast("Gauß, Carl"), "gauß");
  eq("firstAuthorLast.unicodeAccentSingle", B.firstAuthorLast("Étienne"), "étienne");
  eq("firstAuthorLast.unicodeNaturalAccent", B.firstAuthorLast("Émile Zola"), "zola");

  // ---- "and others" ----
  eq("firstAuthorLast.andOthersComma", B.firstAuthorLast("Smith, John and others"), "smith");
  eq("firstAuthorLast.andOthersNatural", B.firstAuthorLast("John Smith and others"), "smith");
  eq("firstAuthorLast.othersAlone", B.firstAuthorLast("others"), "others");

  // ---- extra whitespace ----
  eq("firstAuthorLast.extraSpaces", B.firstAuthorLast("   John   Smith   "), "smith");
  eq("firstAuthorLast.tabSeparated", B.firstAuthorLast("John\tSmith"), "smith");

  // ---- several " and " separators + case-insensitive AND ----
  eq("firstAuthorLast.manySeparators", B.firstAuthorLast("A B and C D and E F"), "b");
  eq("firstAuthorLast.manySeparatorsWithOthers", B.firstAuthorLast("X Y and A B and others"), "y");
  eq("firstAuthorLast.uppercaseAND", B.firstAuthorLast("Smith AND Jones"), "smith");
  eq("firstAuthorLast.mixedCaseAnd", B.firstAuthorLast("Smith aNd Jones"), "smith");

  // ---- numeric / non-string coercion ----
  eq("firstAuthorLast.numericString", B.firstAuthorLast(123), "123");

  // ---- via parseBib entry object ----
  var _e = B.parseBib("@article{k, author={Curie, Marie}, year={1900}}").entries[0];
  eq("firstAuthorLast.viaParseBib", B.firstAuthorLast(B.field(_e,"author")), "curie");

  // ---- robustness: never throws on odd inputs ----
  noThrow("firstAuthorLast.noThrowUndefined", function(){ B.firstAuthorLast(undefined); });
  noThrow("firstAuthorLast.noThrowEmpty", function(){ B.firstAuthorLast(""); });
  noThrow("firstAuthorLast.noThrowWeird", function(){ B.firstAuthorLast("{}\\.,and "); });

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
