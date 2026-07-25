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
  eq("stripTex.empty", B.stripTex(""), "");
  eq("stripTex.undefined", B.stripTex(undefined), "");
  eq("stripTex.null", B.stripTex(null), "");
  eq("stripTex.zero", B.stripTex(0), "");
  eq("stripTex.false", B.stripTex(false), "");
  eq("stripTex.plain", B.stripTex("hello"), "hello");
  eq("stripTex.number_string_kept", B.stripTex("123"), "123");
  eq("stripTex.number_type", B.stripTex(42), "42");
  eq("stripTex.command_braces", B.stripTex("\\textbf{hello}"), "hello");
  eq("stripTex.command_only", B.stripTex("\\textbf"), "");
  eq("stripTex.math_dollar_caret_kept", B.stripTex("$x^2$"), "x^2");
  eq("stripTex.dollar_between", B.stripTex("a$b$c"), "a b c");
  eq("stripTex.nested_braces", B.stripTex("{{a}}"), "a");
  eq("stripTex.multiple_commands", B.stripTex("\\textit{a}\\textbf{b}"), "a b");
  eq("stripTex.whitespace_collapse", B.stripTex("a  b"), "a b");
  eq("stripTex.trim", B.stripTex("  trim me  "), "trim me");
  eq("stripTex.tabs_newlines", B.stripTex("a\tb\nc"), "a b c");
  eq("stripTex.command_greedy_ws", B.stripTex("\\LaTeX   world"), "world");
  eq("stripTex.command_consumes_space", B.stripTex("\\alpha beta"), "beta");
  eq("stripTex.unicode_accented_kept", B.stripTex("café"), "café");
  eq("stripTex.unicode_cjk_kept", B.stripTex("日本語"), "日本語");
  eq("stripTex.accent_apostrophe", B.stripTex("\\'e"), "'e");
  eq("stripTex.accent_cedilla", B.stripTex("\\c{c}"), "c");
  eq("stripTex.frac", B.stripTex("\\frac{a}{b}"), "a b");
  eq("stripTex.nested_commands", B.stripTex("\\emph{\\textbf{x}}"), "x");
  eq("stripTex.lone_backslash_space", B.stripTex("\\ x"), "x");
  eq("stripTex.caret_kept", B.stripTex("^"), "^");
  eq("stripTex.underscore_kept", B.stripTex("a_b"), "a_b");
  eq("stripTex.trailing_command", B.stripTex("word \\ldots"), "word");
  eq("stripTex.only_braces", B.stripTex("{}"), "");
  eq("stripTex.only_command", B.stripTex("\\alpha"), "");
  eq("stripTex.idempotent_plain", B.stripTex(B.stripTex("Hello World")), "Hello World");
  noThrow("stripTex.no_throw_undefined", function(){ B.stripTex(undefined); });
  noThrow("stripTex.no_throw_object", function(){ B.stripTex({}); });
  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
