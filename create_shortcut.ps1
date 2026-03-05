$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Upbit 자동매매.lnk")
$Shortcut.TargetPath = "C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins\run.bat"
$Shortcut.WorkingDirectory = "C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins"
$Shortcut.WindowStyle = 1
$Shortcut.IconLocation = "C:\Windows\System32\cmd.exe,0"
$Shortcut.Description = "Upbit 자동매매 시스템"
$Shortcut.Save()
Write-Host "바로가기 생성 완료"
