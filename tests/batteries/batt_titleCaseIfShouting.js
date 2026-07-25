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
  eq("titleCaseIfShouting.longShout", B.titleCaseIfShouting("HELLO WORLD FROM SPACE"), "Hello World From Space");
  eq("titleCaseIfShouting.threeWordShout", B.titleCaseIfShouting("THE QUICK BROWN"), "The Quick Brown");
  eq("titleCaseIfShouting.singleWordShout", B.titleCaseIfShouting("HELLO"), "Hello");
  eq("titleCaseIfShouting.normalTitleUnchanged", B.titleCaseIfShouting("Hello World"), "Hello World");
  eq("titleCaseIfShouting.mixedCaseUnchanged", B.titleCaseIfShouting("iPhone Review Guide"), "iPhone Review Guide");
  eq("titleCaseIfShouting.shortTwoLetters", B.titleCaseIfShouting("HI"), "HI");
  eq("titleCaseIfShouting.threeLettersUnchanged", B.titleCaseIfShouting("ABC"), "ABC");
  eq("titleCaseIfShouting.fourLettersShout", B.titleCaseIfShouting("ABCD"), "Abcd");
  eq("titleCaseIfShouting.empty", B.titleCaseIfShouting(""), "");
  eq("titleCaseIfShouting.numbersOnly", B.titleCaseIfShouting("1234567"), "1234567");
  eq("titleCaseIfShouting.punctAndNumbers", B.titleCaseIfShouting("HELLO, WORLD! 123"), "Hello, World! 123");
  eq("titleCaseIfShouting.hyphenNotDelimiter", B.titleCaseIfShouting("HELLO-WORLD"), "Hello-world");
  eq("titleCaseIfShouting.parenDelimiter", B.titleCaseIfShouting("(HELLO WORLD)"), "(Hello World)");
  eq("titleCaseIfShouting.braceDelimiter", B.titleCaseIfShouting("{HELLO WORLD}"), "{Hello World}");
  eq("titleCaseIfShouting.leadingSpaces", B.titleCaseIfShouting("  HELLO WORLD  "), "  Hello World  ");
  eq("titleCaseIfShouting.tabDelimiter", B.titleCaseIfShouting("HELLO\tWORLD"), "Hello\tWorld");
  eq("titleCaseIfShouting.newlineDelimiter", B.titleCaseIfShouting("HELLO\nWORLD"), "Hello\nWorld");
  eq("titleCaseIfShouting.digitsBetweenLetters", B.titleCaseIfShouting("A1B2C3D"), "A1b2c3d");
  eq("titleCaseIfShouting.singleLowercasePrevents", B.titleCaseIfShouting("HELLo WORLD"), "HELLo WORLD");
  eq("titleCaseIfShouting.unicodeAccentsStripped", B.titleCaseIfShouting("CAFÉ RÉSUMÉ"), "Café Résumé");
  eq("titleCaseIfShouting.unicodeOnlyUnchanged", B.titleCaseIfShouting("ÉÀÜÖ"), "ÉÀÜÖ");
  eq("titleCaseIfShouting.shortWithNumbers", B.titleCaseIfShouting("AB12"), "AB12");
  eq("titleCaseIfShouting.allSpacesUnchanged", B.titleCaseIfShouting("    "), "    ");
  eq("titleCaseIfShouting.colonSubtitle", B.titleCaseIfShouting("DNA: THE SECRET"), "Dna: The Secret");
  eq("titleCaseIfShouting.hyphenFourLetters", B.titleCaseIfShouting("AB-CD"), "Ab-cd");
  ok("titleCaseIfShouting.returnsString", typeof B.titleCaseIfShouting("HELLO WORLD")==="string", "not string");
  throws("titleCaseIfShouting.throwsOnNull", function(){ B.titleCaseIfShouting(null); });
  throws("titleCaseIfShouting.throwsOnNumber", function(){ B.titleCaseIfShouting(123); });
  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
