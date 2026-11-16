**free
// program CALC from one source; finds GREET through the binding directory.
// call_program {"program": "MYLIB/CALC", "params": [{"type": "char(10)", "value": "Ada"},
//   {"type": "packed(7,2)", "value": 1}, {"type": "packed(7,2)", "value": 2},
//   {"type": "packed(9,2)", "out": true}, {"type": "char(20)", "out": true}]}
ctl-opt dftactgrp(*no) actgrp(*new);
/copy inc/greet_h.rpgleinc
dcl-pi *n;
  name char(10);
  a packed(7:2);
  b packed(7:2);
  sum packed(9:2);
  msg char(20);
end-pi;
sum = a + b;
msg = greet(%trim(name));
*inlr = *on;
