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

  // --- normal cases ---
  eq("protectTitle.singleAcronymDNA", B.protectTitle("The DNA structure"), "The {DNA} structure");
  eq("protectTitle.multipleAcronyms", B.protectTitle("RNA and DNA sequencing"), "{RNA} and {DNA} sequencing");
  eq("protectTitle.leadingAcronym", B.protectTitle("DNA is important"), "{DNA} is important");
  eq("protectTitle.trailingAcronym", B.protectTitle("study of DNA"), "study of {DNA}");
  eq("protectTitle.twoLetterAcronyms", B.protectTitle("MRI and CT scans"), "{MRI} and {CT} scans");
  eq("protectTitle.xmlHtml", B.protectTitle("XML and HTML"), "{XML} and {HTML}");
  eq("protectTitle.acronymAmongLowercase", B.protectTitle("NASA mission"), "{NASA} mission");

  // --- already braced / mixed with braces ---
  eq("protectTitle.alreadyBracedLeftAlone", B.protectTitle("The {DNA} helix"), "The {DNA} helix");
  eq("protectTitle.bracedPlusUnbraced", B.protectTitle("{DNA} and RNA"), "{DNA} and {RNA}");

  // --- all-lowercase untouched ---
  eq("protectTitle.allLowercaseUntouched", B.protectTitle("a study of things"), "a study of things");

  // --- whole all-caps title: early return, left entirely alone ---
  eq("protectTitle.wholeAllCapsEarlyReturn", B.protectTitle("DNA RNA PROTEIN"), "DNA RNA PROTEIN");
  eq("protectTitle.singleAllCapsWordEarlyReturn", B.protectTitle("NASA"), "NASA");
  eq("protectTitle.allCapsWithDigitsEarlyReturn", B.protectTitle("THE 2020 REPORT"), "THE 2020 REPORT");
  eq("protectTitle.allCapsLettersEvenWithBrace", B.protectTitle("{DNA} RNA"), "{DNA} RNA");

  // --- hyphenated: an acronym run protects the whole token ---
  eq("protectTitle.hyphenatedAllCaps", B.protectTitle("MODIS-AQUA data"), "{MODIS-AQUA} data");
  eq("protectTitle.hyphenatedAcronymRun", B.protectTitle("RNA-seq analysis"), "{RNA-seq} analysis");
  eq("protectTitle.hyphenatedCovid", B.protectTitle("COVID-19 pandemic"), "{COVID-19} pandemic");
  eq("protectTitle.hyphenatedAntiCrispr", B.protectTitle("Anti-CRISPR proteins"), "{Anti-CRISPR} proteins");
  eq("protectTitle.hyphenatedNormalWordUntouched", B.protectTitle("well-known result"), "well-known result");
  eq("protectTitle.mRNAinternalCap", B.protectTitle("mRNA vaccine"), "{mRNA} vaccine");

  // --- acronyms with digits (actual behavior: digits stripped from core) ---
  eq("protectTitle.digitH2O", B.protectTitle("H2O molecule"), "{H2O} molecule");
  eq("protectTitle.digitCO2WithAcronym", B.protectTitle("CO2 emissions and DNA"), "{CO2} emissions and {DNA}");
  eq("protectTitle.digitLeadingSingleLetterUntouched", B.protectTitle("3D printing"), "3D printing");

  // --- internal-capital tokens (camelCase style) ---
  eq("protectTitle.internalCapLaTeX", B.protectTitle("The LaTeX system"), "The {LaTeX} system");
  eq("protectTitle.internalCapiPhone", B.protectTitle("iPhone review"), "{iPhone} review");

  // --- single-letter caps not protected (core length < 2) ---
  eq("protectTitle.singleLetterCapsUntouched", B.protectTitle("A Study of X"), "A Study of X");

  // --- punctuation attached to acronym ---
  eq("protectTitle.trailingPeriodInsideBraces", B.protectTitle("about DNA."), "about {DNA.}");
  eq("protectTitle.possessiveAcronymProtected", B.protectTitle("the DNA's role"), "the {DNA's} role");

  // --- math / special chars present ---
  eq("protectTitle.mathLeftAlone", B.protectTitle("The $E=mc^2$ equation"), "The $E=mc^2$ equation");

  // --- unicode ---
  eq("protectTitle.unicodeAccentWord", B.protectTitle("Café DNA"), "Café {DNA}");
  eq("protectTitle.tabWhitespaceSeparator", B.protectTitle("study\tDNA"), "study\t{DNA}");

  // --- empties / falsy / numeric ---
  eq("protectTitle.emptyString", B.protectTitle(""), "");
  eq("protectTitle.nullPassthrough", B.protectTitle(null), null);
  eq("protectTitle.undefinedPassthrough", B.protectTitle(undefined), undefined);
  eq("protectTitle.zeroPassthrough", B.protectTitle(0), 0);
  eq("protectTitle.whitespaceOnly", B.protectTitle("   "), "   ");
  eq("protectTitle.numericOnlyString", B.protectTitle("12345"), "12345");

  // --- adversarial / malformed inputs ---
  throws("protectTitle.numberThrows", function(){ B.protectTitle(123); });
  throws("protectTitle.objectThrows", function(){ B.protectTitle({}); });
  noThrow("protectTitle.longMixedNoThrow", function(){ B.protectTitle("A very long DNA and RNA and PCR title with HTML and XML tokens"); });
  eq("protectTitle.longMixed", B.protectTitle("A very long DNA and RNA and PCR title with HTML and XML tokens"), "A very long {DNA} and {RNA} and {PCR} title with {HTML} and {XML} tokens");

  // --- built via parseBib ---
  var pe = B.parseBib("@article{k, title={The RNA world}, year={2021}}").entries[0];
  eq("protectTitle.fromParseBibTitle", B.protectTitle(B.field(pe,"title")), "The {RNA} world");

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
