# Verdi Interaction

Use this reference only when the user needs an already running Verdi GUI controlled programmatically.

## Tk Send

The reference pattern starts Verdi with a unique Tk name and uses a small Tcl wrapper:

```tcl
send <tk-name> <verdi-command>
```

Before sending commands, confirm the target Verdi session and Tk name. Avoid sending destructive GUI commands unless the user requested them.

## IPython Wrapper

A Python wrapper can launch Verdi, collect available Tcl commands from Verdi documentation, and expose small methods in IPython. Keep this optional because it depends on `VERDI_HOME`, `wish`, Tk send support, and a graphical session.

## Safety

Prefer generated RC files for deterministic waveform layouts. Use interactive control only for inspection, triage, or user-requested GUI automation.

Tk/IPython support in this skill is a guarded command-construction workflow unless a live GUI session, DISPLAY/VNC route, Tk name, and user-approved command target have been verified. Do not imply full automated GUI coverage from a dry-run command plan.
