// Use the Unicode Shell Link API: WScript.Shell can lose non-ANSI path characters.
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace SpectralSweep {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    internal class ShellLink { }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("000214F9-0000-0000-C000-000000000046")]
    internal interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length, IntPtr data, uint flags);
        void GetIDList(out IntPtr pidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int length);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int length);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int command);
        void SetShowCmd(int command);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct PropertyKey {
        public Guid Format;
        public uint Id;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct PropVariant {
        public ushort Type;
        public ushort Reserved1, Reserved2, Reserved3;
        public IntPtr Text;
        public IntPtr Reserved4;
    }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    internal interface IPropertyStore {
        uint GetCount();
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }

    public static class SourceShortcut {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(
            string path, uint access, uint sharing, IntPtr security,
            uint disposition, uint flags, IntPtr template);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandle(
            SafeFileHandle file, StringBuilder path, uint length, uint flags);

        public static string ApplicationId(string directory) {
            const uint openExisting = 3;
            const uint openDirectory = 0x02000000;
            const uint shareReadWriteDelete = 7;
            // Resolve junctions and use the same DOS path form as Python Path.resolve().
            string path;
            using (var handle = CreateFile(directory, 0, shareReadWriteDelete, IntPtr.Zero,
                                           openExisting, openDirectory, IntPtr.Zero)) {
                if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                var buffer = new StringBuilder(260);
                uint count;
                while ((count = GetFinalPathNameByHandle(handle, buffer, (uint)buffer.Capacity, 0))
                       >= (uint)buffer.Capacity) {
                    buffer.Capacity = checked((int)count + 1);
                }
                if (count == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
                path = buffer.ToString();
            }
            if (path.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase))
                path = @"\\" + path.Substring(8);
            else if (path.StartsWith(@"\\?\", StringComparison.Ordinal))
                path = path.Substring(4);

            // Fold ASCII the same way as ui/application_identity.py after resolving the path.
            char[] normalized = path.ToCharArray();
            for (int index = 0; index < normalized.Length; index++) {
                if (normalized[index] >= 'A' && normalized[index] <= 'Z')
                    normalized[index] = (char)(normalized[index] + ('a' - 'A'));
            }
            using (var sha = SHA256.Create()) {
                var digest = sha.ComputeHash(Encoding.UTF8.GetBytes(new string(normalized)));
                var folderId = BitConverter.ToString(digest, 0, 8).Replace("-", "").ToLowerInvariant();
                return "SpectralSweep.SpectralSweep.Source." + folderId;
            }
        }

        public static void Save(string file, string target, string arguments, string directory,
                                string icon, string appId) {
            var link = (IShellLinkW)new ShellLink();
            try {
                link.SetPath(target);
                link.SetArguments(arguments);
                link.SetWorkingDirectory(directory);
                link.SetIconLocation(icon, 0);
                link.SetDescription("Update main safely, then start SpectralSweep from this source folder.");
                link.SetShowCmd(1);
                var store = (IPropertyStore)link;
                var key = new PropertyKey {
                    Format = new Guid("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), Id = 5
                };
                var value = new PropVariant { Type = 31, Text = Marshal.StringToCoTaskMemUni(appId) };
                try {
                    store.SetValue(ref key, ref value);
                    store.Commit();
                } finally {
                    Marshal.FreeCoTaskMem(value.Text);
                }
                ((IPersistFile)link).Save(file, true);
            } finally {
                Marshal.FinalReleaseComObject(link);
            }
        }
    }
}
