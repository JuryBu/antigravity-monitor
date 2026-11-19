Option Explicit
Dim fso, shell, appDir, pythonwPath, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)

Function FindPythonW()
    Dim localApp, progFiles, candidates, item
    localApp = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
    progFiles = shell.ExpandEnvironmentStrings("%ProgramFiles%")
    candidates = Array( _
        appDir & "\.venv\Scripts\pythonw.exe", _
        appDir & "\venv\Scripts\pythonw.exe", _
        localApp & "\Programs\Python\Python314\pythonw.exe", _
        localApp & "\Programs\Python\Python313\pythonw.exe", _
        localApp & "\Programs\Python\Python312\pythonw.exe", _
        localApp & "\Programs\Python\Python311\pythonw.exe", _
        localApp & "\Programs\Python\Python310\pythonw.exe", _
        progFiles & "\Python314\pythonw.exe", _
        progFiles & "\Python313\pythonw.exe", _
        progFiles & "\Python312\pythonw.exe", _
        progFiles & "\Python311\pythonw.exe", _
        progFiles & "\Python310\pythonw.exe", _
        localApp & "\Programs\Python\Python39\pythonw.exe" _
    )
    For Each item In candidates
        If fso.FileExists(item) Then
            FindPythonW = """" & item & """"
            Exit Function
        End If
    Next
    FindPythonW = "pythonw.exe"
End Function

On Error Resume Next
shell.Environment("Process")("PYTHONUTF8") = ""
On Error GoTo 0

pythonwPath = FindPythonW()
shell.CurrentDirectory = appDir
cmd = pythonwPath & " """ & appDir & "\main.py"""
shell.Run cmd, 0, False
