**free
// module MAIN, bound into program MAIN with service program GREET
dcl-f custl1 keyed usage(*input);
/copy inc/greet_h.rpgleinc
dcl-pi *n;
  name char(10);
  msg char(50);
end-pi;
msg = greet(%trim(name));
*inlr = *on;
