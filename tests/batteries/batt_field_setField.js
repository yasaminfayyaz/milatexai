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

  function E(s){ return B.parseBib(s).entries[0]; }

  // --- field(): get existing values ---
  eq("field_setField.get_existing_title", B.field(E("@article{k,title={Hello World},author={Doe, John},year={2020}}"),"title"), "Hello World");
  eq("field_setField.get_existing_author", B.field(E("@article{k,title={T},author={Doe, John}}"),"author"), "Doe, John");
  eq("field_setField.get_existing_year", B.field(E("@article{k,year={2020}}"),"year"), "2020");

  // --- field(): missing -> "" ---
  eq("field_setField.get_missing_returns_empty", B.field(E("@article{k,title={T}}"),"nosuch"), "");
  eq("field_setField.get_missing_on_no_fields", B.field(E("@misc{k}"),"title"), "");

  // --- case sensitivity: parseBib lowercases names ---
  eq("field_setField.parse_lowercases_name", B.field(E("@article{k,TITLE={X}}"),"title"), "X");
  eq("field_setField.get_case_sensitive_upper_missing", B.field(E("@article{k,title={X}}"),"Title"), "");

  // --- duplicate field kept first ---
  eq("field_setField.duplicate_kept_first", B.field(E("@article{k,title={A},title={B}}"),"title"), "A");

  // --- value normalization by parser (whitespace collapse) ---
  eq("field_setField.whitespace_collapsed", B.field(E("@article{k,title={Hello    World}}"),"title"), "Hello World");
  eq("field_setField.empty_braces_value", B.field(E("@article{k,title={}}"),"title"), "");

  // --- unicode preserved ---
  eq("field_setField.unicode_value_preserved", B.field(E("@article{k,title={cafe é ☕ Üni}}"),"title"), "cafe é ☕ Üni");

  // --- setField(): update existing in place ---
  (function(){ var e=E("@article{k,title={Old},year={2020}}"); B.setField(e,"title","New");
    eq("field_setField.setField_updates_existing", B.field(e,"title"), "New");
    eq("field_setField.setField_update_no_length_change", e.fields.length, 2);
    eq("field_setField.setField_update_leaves_other", B.field(e,"year"), "2020"); })();

  // --- setField(): append when absent ---
  (function(){ var e=E("@article{k,title={T}}"); B.setField(e,"note","hi");
    eq("field_setField.setField_appends_when_absent", B.field(e,"note"), "hi");
    eq("field_setField.setField_append_increases_length", e.fields.length, 2);
    eqJSON("field_setField.setField_append_object_shape", e.fields[e.fields.length-1], {name:"note",value:"hi"}); })();

  // --- setField(): empty value ---
  (function(){ var e=E("@article{k,title={T}}"); B.setField(e,"title","");
    eq("field_setField.setField_empty_value", B.field(e,"title"), ""); })();

  // --- setField(): case-sensitive -> creates a distinct field ---
  (function(){ var e=E("@article{k,title={low}}"); B.setField(e,"Title","up");
    eq("field_setField.setField_case_creates_new_len", e.fields.length, 2);
    eq("field_setField.setField_case_new_value", B.field(e,"Title"), "up");
    eq("field_setField.setField_case_orig_untouched", B.field(e,"title"), "low"); })();

  // --- setField(): non-string value preserved by identity ---
  (function(){ var e=E("@article{k,title={T}}"); B.setField(e,"count",42);
    eq("field_setField.setField_numeric_value_strict", B.field(e,"count"), 42); })();

  // --- setField(): returns undefined ---
  (function(){ var e=E("@article{k,title={T}}");
    eq("field_setField.setField_returns_undefined", B.setField(e,"a","b"), undefined); })();

  // --- setField(): unicode append ---
  (function(){ var e=E("@article{k,title={T}}"); B.setField(e,"note","☃ snøw");
    eq("field_setField.setField_unicode_append", B.field(e,"note"), "☃ snøw"); })();

  // --- setField on empty fields array appends ---
  (function(){ var e=E("@misc{k}"); B.setField(e,"title","Z");
    eq("field_setField.setField_on_empty_array_appends", B.field(e,"title"), "Z");
    eq("field_setField.setField_on_empty_array_len", e.fields.length, 1); })();

  // --- adversarial / malformed inputs ---
  eq("field_setField.field_plain_object_no_fields_empty", B.field({},"title"), "");
  eq("field_setField.field_fields_empty_array_empty", B.field({fields:[]},"x"), "");
  throws("field_setField.field_null_entry_throws", function(){ B.field(null,"title"); });
  throws("field_setField.setField_no_fields_prop_throws", function(){ B.setField({},"a","b"); });
  noThrow("field_setField.setField_on_parsed_ok", function(){ B.setField(E("@article{k,title={T}}"),"x","y"); });

  return JSON.stringify({pass:pass,fail:fail,total:pass+fail,failures:R});
})();
