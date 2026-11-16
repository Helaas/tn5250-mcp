**free
// module GREET, exported by service program GREET (see greet.bnd)
ctl-opt nomain;
/copy inc/greet_h.rpgleinc
dcl-proc greet export;
  dcl-pi *n varchar(50);
    name varchar(20) const;
  end-pi;
  return 'Hello ' + %trim(name);
end-proc;
