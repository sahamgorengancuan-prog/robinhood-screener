#!/bin/sh
# =============================================================================
#   EMERGENCY STOP — halts all order execution immediately.
#
#   Run it:   ./emergency-stop.sh      (or:  sh emergency-stop.sh)
#
#   This writes the KILL_SWITCH sentinel file. It uses /bin/sh only: no Python,
#   no virtualenv, no running service, no network. That is the point — it has to
#   work when everything else is broken, which is exactly when you need it.
#
#   Screening and alerting keep running. Only execution stops.
#   Undo it from the control panel (./start.sh -> "Risiko & Order" tab), or by
#   deleting the KILL_SWITCH file in this folder.
# =============================================================================

# Deliberately POSIX sh with no `set -e`: this script must reach its own error
# handling rather than abort silently on the first failed write.

cd "$(dirname "$0")" || exit 1

echo "engaged by emergency-stop.sh at $(date -u '+%Y-%m-%dT%H:%M:%SZ')" > KILL_SWITCH 2>/dev/null

if [ ! -f KILL_SWITCH ]; then
    echo ""
    echo "  [x] Could not create the KILL_SWITCH file in:"
    echo "      $(pwd)"
    echo ""
    echo "  Check the folder permissions. Alternative ways to stop execution:"
    echo "    - press Ctrl+C in the window running the panel"
    echo "    - set KILL_SWITCH=true in .env"
    echo ""
    exit 1
fi

echo ""
echo "  ##################################################################"
echo "  #                                                                #"
echo "  #    KILL SWITCH ENGAGED - no further orders can be placed       #"
echo "  #                                                                #"
echo "  ##################################################################"
echo ""
echo "  A running service picks this up on its next check; no restart needed."
echo "  Screening and alerts continue as normal."
echo ""
echo "  To resume: open the panel with ./start.sh and use the"
echo "  \"Risiko & Order\" tab, or delete the KILL_SWITCH file here."
echo ""
