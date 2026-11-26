using System;
using System.Text;
using System.Runtime.InteropServices;

// Private to the QA window identified by owner and class in the WinUI probe.
public static class OwnedWindow {
    [DllImport("user32")] public static extern IntPtr GetWindow(IntPtr h, uint cmd);
    [DllImport("user32")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32")] static extern IntPtr GetParent(IntPtr h);
    [DllImport("user32")] static extern int GetDlgCtrlID(IntPtr h);
    [DllImport("user32")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32")] public static extern bool PostMessage(IntPtr h,uint msg,IntPtr wp,IntPtr lp);
    [DllImport("user32",CharSet=CharSet.Unicode)] static extern IntPtr SendMessage(IntPtr h,uint msg,IntPtr wp,string value);
    [DllImport("user32",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h,StringBuilder value,int max);
    delegate bool EnumProc(IntPtr child,IntPtr data);
    [DllImport("user32")] static extern bool EnumChildWindows(IntPtr root,EnumProc callback,IntPtr data);

    public static bool SetFilename(IntPtr dialog,string path) {
        IntPtr edit=IntPtr.Zero;
        EnumChildWindows(dialog, delegate(IntPtr child,IntPtr data) {
            var name=new StringBuilder(64);
            GetClassName(child,name,name.Capacity);
            if (name.ToString().Equals("Edit",StringComparison.OrdinalIgnoreCase)) {
                for(IntPtr parent=child; parent!=IntPtr.Zero && parent!=dialog; parent=GetParent(parent)) {
                    if(GetDlgCtrlID(parent)==1148 || GetDlgCtrlID(parent)==1152) {
                        edit=child;
                        return false;
                    }
                }
            }
            return true;
        },IntPtr.Zero);
        return edit!=IntPtr.Zero && SendMessage(edit,12,IntPtr.Zero,path)!=IntPtr.Zero;
    }

    public static bool FinishTextPicker(IntPtr dialog,string path) {
        if(GetForegroundWindow()!=dialog) return false;
        if(string.IsNullOrEmpty(path)) return PostMessage(dialog,0x10,IntPtr.Zero,IntPtr.Zero);
        IntPtr edit=IntPtr.Zero,ok=IntPtr.Zero;
        EnumChildWindows(dialog, delegate(IntPtr child,IntPtr ignored) {
            if(!IsWindowVisible(child)) return true;
            var name=new StringBuilder(64);
            GetClassName(child,name,name.Capacity);
            int id=GetDlgCtrlID(child);
            if(name.ToString()=="Button" && id==1) ok=child;
            if(name.ToString()=="Edit" && id==1001) {
                GetClassName(GetParent(child),name,name.Capacity);
                if(name.ToString()=="ComboBox") edit=child;
            }
            return true;
        },IntPtr.Zero);
        return edit!=IntPtr.Zero && ok!=IntPtr.Zero && SendMessage(edit,12,IntPtr.Zero,path)!=IntPtr.Zero &&
               PostMessage(ok,0xF5,IntPtr.Zero,IntPtr.Zero);
    }
}
