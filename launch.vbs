Dim shell
Set shell = CreateObject("WScript.Shell")

' Launch the MindHub Trader desktop app (handles dashboard, bot, and tray internally)
shell.Run "cmd /c cd /d C:\Users\brend\FeintTrade2 && python app.py", 0, False

' Run immediate crypto cycle in background
shell.Run "cmd /c cd /d C:\Users\brend\FeintTrade2 && python scripts\orchestrator.py crypto >> logs\crypto.log 2>&1", 0, False

Set shell = Nothing
