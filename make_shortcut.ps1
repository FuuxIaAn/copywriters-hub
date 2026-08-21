$s = (New-Object -COM WScript.Shell).CreateShortcut("C:\Users\linsh\Desktop\靓仔文案工作台.lnk")
$s.TargetPath = "C:\Users\linsh\Desktop\靓仔文案工作台.exe"
$s.WorkingDirectory = "C:\Users\linsh\Desktop"
$s.IconLocation = "C:\Users\linsh\Desktop\靓仔文案工作台.exe,0"
$s.Save()
