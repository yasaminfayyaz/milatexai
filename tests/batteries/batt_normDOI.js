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

  // --- empties / falsy ---
  eq("normDOI.empty_string", B.normDOI(""), "");
  eq("normDOI.null", B.normDOI(null), "");
  eq("normDOI.undefined", B.normDOI(undefined), "");
  eq("normDOI.whitespace_only_becomes_empty", B.normDOI("   "), "");
  eq("normDOI.tabs_newlines_only", B.normDOI("\t\n \r"), "");

  // --- https / http / dx prefixes stripped ---
  eq("normDOI.https_doi_org", B.normDOI("https://doi.org/10.1000/xyz"), "10.1000/xyz");
  eq("normDOI.http_doi_org", B.normDOI("http://doi.org/10.1000/xyz"), "10.1000/xyz");
  eq("normDOI.https_dx_doi_org", B.normDOI("https://dx.doi.org/10.1000/xyz"), "10.1000/xyz");
  eq("normDOI.http_dx_doi_org", B.normDOI("http://dx.doi.org/10.1000/xyz"), "10.1000/xyz");
  eq("normDOI.prefix_case_insensitive", B.normDOI("HTTPS://DOI.ORG/10.1/ABC"), "10.1/abc");
  eq("normDOI.dx_prefix_uppercase", B.normDOI("HTTP://DX.DOI.ORG/10.5/DeF"), "10.5/def");

  // --- doi: prefix ---
  eq("normDOI.doi_colon_lower", B.normDOI("doi:10.1000/xyz"), "10.1000/xyz");
  eq("normDOI.doi_colon_upper", B.normDOI("DOI:10.1000/XYZ"), "10.1000/xyz");
  eq("normDOI.doi_colon_space_kept", B.normDOI("doi: 10.1000/xyz"), " 10.1000/xyz");

  // --- uppercase / whitespace ---
  eq("normDOI.uppercase_lowered", B.normDOI("10.1000/ABCdef"), "10.1000/abcdef");
  eq("normDOI.surrounding_whitespace_trimmed", B.normDOI("  10.1000/xyz  "), "10.1000/xyz");
  eq("normDOI.trim_then_strip_prefix", B.normDOI("  https://doi.org/10.5555/ABC  "), "10.5555/abc");
  eq("normDOI.leading_tab", B.normDOI("\t10.1/x\n"), "10.1/x");

  // --- already normalized ---
  eq("normDOI.already_normalized", B.normDOI("10.1000/xyz123"), "10.1000/xyz123");
  eq("normDOI.idempotent", B.normDOI(B.normDOI("https://doi.org/10.1/AbC")), "10.1/abc");

  // --- trailing slash / punctuation kept (test actual) ---
  eq("normDOI.trailing_slash_kept", B.normDOI("https://doi.org/10.1/abc/"), "10.1/abc/");
  eq("normDOI.trailing_period_kept", B.normDOI("10.1000/xyz."), "10.1000/xyz.");
  eq("normDOI.internal_punct_kept", B.normDOI("10.1000/(sici)1097"), "10.1000/(sici)1097");

  // --- non-DOI strings pass through (lowercased) ---
  eq("normDOI.non_doi_string", B.normDOI("hello world"), "hello world");
  eq("normDOI.www_doi_org_not_stripped", B.normDOI("https://www.doi.org/10.1/x"), "https://www.doi.org/10.1/x");
  eq("normDOI.no_trailing_slash_url_kept", B.normDOI("https://doi.org"), "https://doi.org");

  // --- adversarial: only leading anchored strip, single occurrence ---
  eq("normDOI.doi_prefix_only_once", B.normDOI("doi:doi:10.1/x"), "doi:10.1/x");
  eq("normDOI.double_url_prefix_single_strip", B.normDOI("https://doi.org/https://doi.org/10.1"), "https://doi.org/10.1");
  eq("normDOI.doi_then_url_url_not_stripped", B.normDOI("doi:https://doi.org/10.1"), "https://doi.org/10.1");
  eq("normDOI.url_then_doi_colon_both_stripped", B.normDOI("https://doi.org/doi:10.1"), "10.1");

  // --- unicode ---
  eq("normDOI.unicode_accent_lowered", B.normDOI("10.1/CafÉ"), "10.1/café");
  eq("normDOI.cyrillic_lowered", B.normDOI("ПРИВЕТ"), "привет");
  eq("normDOI.emoji_passthrough", B.normDOI("10.1/😀"), "10.1/😀");

  // --- non-string truthy input throws (no .trim) ---
  throws("normDOI.number_input_throws", function(){ B.normDOI(12345); });

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
