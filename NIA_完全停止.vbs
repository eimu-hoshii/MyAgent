Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = baseDir
cmd = "pythonw """ & baseDir & "\scripts\launcher.pyw"" --mode=stop"
On Error Resume Next
shell.Run cmd, 0, False
If Err.Number <> 0 Then
    Err.Clear
    cmdFallback = "python """ & baseDir & "\scripts\launcher.pyw"" --mode=stop"
    shell.Run cmdFallback, 0, False
End If
