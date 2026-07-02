# Verdi RC Scenario Format

`scripts/python/rc/generate_rc.py` reads a config directory with `scn_base.lst` and `scn_<scenario>.lst`.

## Base File

```ini
[unit]
top = tb.u_top
clk = tb.clk
rst = tb.rst_n
```

Keys are aliases used by scenario signal paths. `top.state[0]` resolves to `tb.u_top.state[0]`.

## Scenario File

```ini
[GROUPS]
1. TIMING gray
1.1 clk bin cyan 12 CLK_ALIAS
2. STATUS yellow
2.1 VBUS_STATE hex yellow 30 SYS_STATE

[VIRTUAL_BUSES]
VBUS_STATE = top.state[2], top.state[1], top.state[0]

[MARKERS]
1000 START white
```

Group headers use `N. NAME [bg_color]`. Signal rows use `N.M path radix color [height] [alias]`.

Supported radix values are `hex`, `bin`, `dec`, `oct`, and `analog`. Unknown colors are omitted rather than guessed.
