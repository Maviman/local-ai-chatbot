using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

// What the file's Properties > Details tab says. The FILE is still called "Local AI ChatBot.exe" (shortcuts point at it).
[assembly: AssemblyTitle("Personal Terminal")]
[assembly: AssemblyProduct("Personal Terminal")]

// The double-click target. It starts the bundled interpreter on app\desktop_main.py and then keeps an eye on it for a few seconds,
// because a copy that is damaged or incomplete fails in ways Windows and Python say nothing useful about under pythonw.exe (no
// console): the program just vanishes. Everything the user needs in that case is said here, in one box.
//   1. Files missing next to this .exe (a partial copy, an update package extracted into an empty folder): named, with what to do.
//   2. The interpreter dies at once with a Windows loader error (0xC0000135 missing DLL, 0xC000012F / 0xC000007B bad image, ...):
//      explained, with the code.
//   3. The interpreter dies at once with a plain nonzero code and never got as far as opening data\desktop.log: Python itself could
//      not start (its standard library is missing or damaged).
//   If Python did get that far, app\desktop_main.py has shown its own message, so nothing more is shown here.
// C# 5 only (the csc.exe that ships with Windows): no string interpolation, no ?. operator.
class Launcher
{
    const string Title = "Personal Terminal";
    const int DefaultWatchMs = 8000;

    // Files whose absence stops the app before it can say anything (relative to this .exe).
    static readonly string[] Essential = new string[]
    {
        @"runtime\python\pythonw.exe",
        @"runtime\python\python312.dll",
        @"runtime\python\python312.zip",
        @"runtime\python\_tkinter.pyd",
        @"runtime\python\tcl86t.dll",
        @"runtime\python\tk86t.dll",
        @"runtime\python\tcl\tcl8.6\init.tcl",
        @"runtime\python\tcl\tk8.6\tk.tcl",
        @"app\desktop_main.py",
        @"app\server.py",
    };

    [STAThread]
    static void Main()
    {
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;
        string pythonw = Path.Combine(exeDir, "runtime", "python", "pythonw.exe");
        string script = Path.Combine(exeDir, "app", "desktop_main.py");

        List<string> missing = new List<string>();
        foreach (string rel in Essential)
        {
            if (!File.Exists(Path.Combine(exeDir, rel)))
            {
                missing.Add(rel);
            }
        }
        if (missing.Count > 0)
        {
            Report(MissingMessage(exeDir, missing));
            return;
        }

        string logPath = Path.Combine(exeDir, "data", "desktop.log");
        long logLength;
        DateTime logTime;
        ReadLog(logPath, out logLength, out logTime);

        Process proc;
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = pythonw;
            psi.Arguments = "\"" + script + "\"";
            psi.WorkingDirectory = exeDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            proc = Process.Start(psi);
        }
        catch (Exception ex)
        {
            Report("Could not start Personal Terminal:\n\n" + ex.Message + "\n\n" +
                   "Its program file may be damaged, or security software may be blocking it. Copy the whole folder again from " +
                   "the original, and allow this folder in your security software." + DiagnoseHint(exeDir));
            return;
        }
        if (proc == null)
        {
            return;
        }

        // Normal case: the program keeps running (this wait ends after the window time-out), or exits 0 (a second launch that only
        // brought the first window to the front).
        int watchMs = DefaultWatchMs;
        string watchOverride = Environment.GetEnvironmentVariable("PT_LAUNCHER_WATCH_MS");
        int parsed;
        if (!String.IsNullOrEmpty(watchOverride) && Int32.TryParse(watchOverride, out parsed) && parsed >= 0)
        {
            watchMs = parsed;
        }
        bool exited;
        try
        {
            exited = proc.WaitForExit(watchMs);
        }
        catch (Exception)
        {
            return;
        }
        if (!exited)
        {
            return;
        }
        int code = proc.ExitCode;
        if (code == 0)
        {
            return;
        }

        uint status = unchecked((uint)code);
        long logLength2;
        DateTime logTime2;
        ReadLog(logPath, out logLength2, out logTime2);
        bool pythonRan = logLength2 != logLength || logTime2 != logTime;

        if (status >= 0xC0000000u)
        {
            Report(EarlyExitMessage(exeDir, status));
        }
        else if (!pythonRan)
        {
            Report("Personal Terminal stopped right after it started (exit code " + code + ") and Python never got going.\n\n" +
                   "The Python files in the 'runtime' folder are missing or damaged. This is nearly always a copy that was cut short " +
                   "or skipped files.\n\nWhat to do: copy the whole Personal Terminal folder again from the original (the 'runtime' " +
                   "folder included), then start it again." + DiagnoseHint(exeDir));
        }
        // else: Python ran and desktop_main.py has already told the user what went wrong.
    }

    static void ReadLog(string path, out long length, out DateTime writeTime)
    {
        length = -1;
        writeTime = DateTime.MinValue;
        try
        {
            FileInfo fi = new FileInfo(path);
            if (fi.Exists)
            {
                length = fi.Length;
                writeTime = fi.LastWriteTimeUtc;
            }
        }
        catch (Exception)
        {
        }
    }

    static string DiagnoseHint(string exeDir)
    {
        if (File.Exists(Path.Combine(exeDir, "Diagnose.bat")))
        {
            return "\n\nTo find out exactly what is wrong, double-click Diagnose.bat in this folder: it checks every file of this " +
                   "installation and writes a report to the 'data' folder that you can send to whoever supports you.";
        }
        return "";
    }

    static string MissingMessage(string exeDir, List<string> missing)
    {
        StringBuilder sb = new StringBuilder();
        sb.Append("Personal Terminal cannot start: some of its files are not next to this program.\n\nMissing:\n");
        int shown = 0;
        foreach (string rel in missing)
        {
            if (shown == 6)
            {
                sb.Append("  ... and " + (missing.Count - shown) + " more\n");
                break;
            }
            sb.Append("  " + rel + "\n");
            shown++;
        }
        sb.Append("\nThis program only works inside the complete Personal Terminal folder, together with its 'runtime' and 'app' " +
                  "folders. Copy the whole folder, not just this .exe.\n\n" +
                  "An update package (a .zip made by Build Update Package.bat) holds only the app's code. Extract it OVER a complete " +
                  "installation (allow overwrite); extracted into an empty folder it cannot run.");
        sb.Append(DiagnoseHint(exeDir));
        return sb.ToString();
    }

    static string EarlyExitMessage(string exeDir, uint status)
    {
        string what;
        string todo;
        switch (status)
        {
            case 0xC0000135u:
                what = "Windows could not find a file the program needs (a DLL).";
                todo = "Install the \"Microsoft Visual C++ Redistributable 2015-2022 (x64)\" from Microsoft, or copy the whole " +
                       "Personal Terminal folder again from the original: a part of it was not copied.";
                break;
            case 0xC000012Fu:
            case 0xC000007Bu:
                what = "One of the program files in this folder is damaged, or is not a Windows program file at all (a copy that was " +
                       "cut short, a placeholder from a cloud-sync folder, or the wrong kind of file).";
                todo = "Copy the whole Personal Terminal folder again from the original, wait until the copy has really finished, " +
                       "and start it again. If it is damaged again right after a fresh copy, suspect the drive, the cable or the " +
                       "PC's memory.";
                break;
            case 0xC0000142u:
                what = "A program file started but could not finish loading.";
                todo = "Security software that blocks unknown programs is the usual cause: allow this folder in it. A missing " +
                       "\"Microsoft Visual C++ Redistributable 2015-2022 (x64)\" can cause it too.";
                break;
            case 0xC0000139u:
                what = "A program file in this folder is the wrong version for the others.";
                todo = "Copy the whole Personal Terminal folder again from the original, so that all files come from the same copy.";
                break;
            default:
                what = "Windows ended the program with an error.";
                todo = "Copy the whole Personal Terminal folder again from the original and start it again. If it happens again, " +
                       "send the code above to whoever supports you.";
                break;
        }
        return "Personal Terminal stopped right after it started (Windows error code 0x" + status.ToString("X8") + ").\n\n" +
               what + "\n\nWhat to do: " + todo + DiagnoseHint(exeDir);
    }

    // The message goes to a box. PT_LAUNCHER_MSGFILE (a test hook, harmless otherwise) appends it to that file instead.
    static void Report(string text)
    {
        string file = Environment.GetEnvironmentVariable("PT_LAUNCHER_MSGFILE");
        if (!String.IsNullOrEmpty(file))
        {
            try
            {
                File.AppendAllText(file, text + "\n=====\n");
            }
            catch (Exception)
            {
            }
            return;
        }
        MessageBox.Show(text, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
