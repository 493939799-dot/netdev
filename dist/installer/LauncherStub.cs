// ═══════════════════════════════════════════════════════════════════════════
//  netdev 工具台 · Windows 图形启动器（netdev-toolbox.exe）
// ───────────────────────────────────────────────────────────────────────────
//  为什么有它：命令行用户敲 `netdev ui` 没问题，但普通用户双击 .cmd 会弹黑窗、
//  看不懂报错。这个启动器把「服务在不在跑 / 打不开怎么修」变成两个按钮。
//
//  设计要点：
//    · /target:winexe —— 全程无控制台窗口，像一个正常桌面应用；
//    · 状态每 3 秒自检一次（TCP + /api/health 双重确认，不看 PID 文件）；
//    · 「一键修复」直接调随包安装的 一键体检.ps1 -Fix，复用同一套修复逻辑；
//    · 所有子进程 CreateNoWindow=true，不会闪窗。
//
//  编译（csc.exe 来自 .NET Framework，Windows 自带）：
//    csc.exe /nologo /target:winexe /codepage:65001 ^
//            /reference:System.Windows.Forms.dll /reference:System.Drawing.dll ^
//            /win32icon:netdev.ico /out:netdev-toolbox.exe LauncherStub.cs
// ═══════════════════════════════════════════════════════════════════════════
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace NetdevToolbox
{
    static class Program
    {
        [STAThread]
        static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
        }
    }

    class MainForm : Form
    {
        // ── 与 netdev_cli.py 保持一致的常量
        const int Port = 8898;
        const string Host = "127.0.0.1";

        readonly string root;
        readonly string pyExe;
        readonly string cliPy;
        readonly string healthPs1;
        readonly string logFile;
        readonly string pidFile;
        readonly string version;

        // ── 控件
        PictureBox picIcon;
        Label lblTitle, lblSubtitle;
        Panel pnlDot;
        Label lblStatusText, lblStatusHint;
        Label lblAddrV, lblPidV, lblVerV, lblAutoV;
        Button btnOpen, btnRestart, btnFix, btnLog, btnDir, btnRefresh;
        TextBox txtLog;

        System.Windows.Forms.Timer timer;
        volatile bool busy;
        bool lastRunning;

        public MainForm()
        {
            root = Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);

            string venvPy = Path.Combine(root, @".venv\Scripts\python.exe");
            string venvPyw = Path.Combine(root, @".venv\Scripts\pythonw.exe");
            pyExe = File.Exists(venvPy) ? venvPy : (File.Exists(venvPyw) ? venvPyw : "python.exe");
            cliPy = Path.Combine(root, "netdev_cli.py");
            healthPs1 = Path.Combine(root, "一键体检.ps1");
            logFile = Path.Combine(root, @"logs\ui-service.log");
            pidFile = Path.Combine(root, @"logs\ui-service.pid");
            version = ReadVersion();

            BuildUi();
            RefreshStatusAsync();
            timer = new System.Windows.Forms.Timer();
            timer.Interval = 3000;
            timer.Tick += delegate { RefreshStatusAsync(); };
            timer.Start();
        }

        // ─────────────────────────────────────────────── 界面
        void BuildUi()
        {
            Text = "netdev 网络设备工具台";
            ClientSize = new Size(560, 544);
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(244, 246, 250);
            Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular, GraphicsUnit.Point);
            AutoScaleMode = AutoScaleMode.Dpi;
            try
            {
                Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            }
            catch { }

            // ── 顶部横幅
            Panel header = new Panel();
            header.BackColor = Color.FromArgb(15, 23, 42);
            header.Location = new Point(0, 0);
            header.Size = new Size(ClientSize.Width, 68);
            header.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            Controls.Add(header);

            picIcon = new PictureBox();
            picIcon.Location = new Point(16, 14);
            picIcon.Size = new Size(40, 40);
            picIcon.SizeMode = PictureBoxSizeMode.Zoom;
            picIcon.BackColor = Color.Transparent;
            try { picIcon.Image = Icon.ExtractAssociatedIcon(Application.ExecutablePath).ToBitmap(); }
            catch { }
            header.Controls.Add(picIcon);

            lblTitle = new Label();
            lblTitle.Text = "netdev 网络设备工具台";
            lblTitle.ForeColor = Color.White;
            lblTitle.Font = new Font("Microsoft YaHei UI", 13F, FontStyle.Bold, GraphicsUnit.Point);
            lblTitle.AutoSize = true;
            lblTitle.BackColor = Color.Transparent;
            lblTitle.Location = new Point(68, 14);
            header.Controls.Add(lblTitle);

            lblSubtitle = new Label();
            lblSubtitle.Text = "网络设备调试 · 人机同屏会话";
            lblSubtitle.ForeColor = Color.FromArgb(148, 163, 184);
            lblSubtitle.AutoSize = true;
            lblSubtitle.BackColor = Color.Transparent;
            lblSubtitle.Location = new Point(70, 42);
            header.Controls.Add(lblSubtitle);

            // ── 状态卡
            Panel card = new Panel();
            card.BackColor = Color.White;
            card.Location = new Point(16, 84);
            card.Size = new Size(ClientSize.Width - 32, 172);
            card.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            card.Paint += delegate(object s, PaintEventArgs e)
            {
                using (Pen p = new Pen(Color.FromArgb(226, 232, 240)))
                    e.Graphics.DrawRectangle(p, 0, 0, card.Width - 1, card.Height - 1);
            };
            Controls.Add(card);

            pnlDot = new Panel();
            pnlDot.Size = new Size(12, 12);
            pnlDot.Location = new Point(20, 24);
            pnlDot.BackColor = Color.FromArgb(148, 163, 184);
            card.Controls.Add(pnlDot);

            lblStatusText = new Label();
            lblStatusText.Text = "正在检查服务状态…";
            lblStatusText.Font = new Font("Microsoft YaHei UI", 12F, FontStyle.Bold, GraphicsUnit.Point);
            lblStatusText.ForeColor = Color.FromArgb(30, 41, 59);
            lblStatusText.AutoSize = true;
            lblStatusText.Location = new Point(40, 18);
            card.Controls.Add(lblStatusText);

            lblStatusHint = new Label();
            lblStatusHint.Text = "";
            lblStatusHint.ForeColor = Color.FromArgb(100, 116, 139);
            lblStatusHint.AutoSize = true;
            lblStatusHint.Location = new Point(42, 46);
            card.Controls.Add(lblStatusHint);

            lblAddrV = AddRow(card, 76, "地址");
            lblPidV = AddRow(card, 98, "进程");
            lblVerV = AddRow(card, 120, "版本");
            lblVerV.Text = version;
            lblAutoV = AddRow(card, 142, "开机自启");

            // ── 按钮
            int y1 = 272;
            btnOpen = AddButton("打开网页界面", 16, y1, 168, Color.FromArgb(22, 163, 74), Color.White);
            btnRestart = AddButton("启动 / 重启服务", 196, y1, 168, Color.White, Color.FromArgb(30, 41, 59));
            btnFix = AddButton("一键修复", 376, y1, 168, Color.White, Color.FromArgb(30, 41, 59));
            int y2 = y1 + 44;
            btnRefresh = AddButton("刷新状态", 16, y2, 168, Color.White, Color.FromArgb(30, 41, 59));
            btnLog = AddButton("查看日志", 196, y2, 168, Color.White, Color.FromArgb(30, 41, 59));
            btnDir = AddButton("打开安装目录", 376, y2, 168, Color.White, Color.FromArgb(30, 41, 59));

            btnOpen.Click += delegate { DoOpen(); };
            btnRestart.Click += delegate { DoRestart(); };
            btnFix.Click += delegate { DoFix(); };
            btnRefresh.Click += delegate { RefreshStatusAsync(); };
            btnLog.Click += delegate { DoOpenLog(); };
            btnDir.Click += delegate { DoOpenDir(); };

            // ── 日志区
            Label lblLogCap = new Label();
            lblLogCap.Text = "运行日志";
            lblLogCap.ForeColor = Color.FromArgb(100, 116, 139);
            lblLogCap.AutoSize = true;
            lblLogCap.Location = new Point(18, y2 + 56);
            Controls.Add(lblLogCap);

            txtLog = new TextBox();
            txtLog.Multiline = true;
            txtLog.ReadOnly = true;
            txtLog.ScrollBars = ScrollBars.Vertical;
            txtLog.BackColor = Color.FromArgb(15, 23, 42);
            txtLog.ForeColor = Color.FromArgb(203, 213, 225);
            txtLog.Font = new Font("Consolas", 8.5F, FontStyle.Regular, GraphicsUnit.Point);
            txtLog.BorderStyle = BorderStyle.FixedSingle;
            txtLog.Location = new Point(16, y2 + 78);
            txtLog.Size = new Size(ClientSize.Width - 32, 108);
            txtLog.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom;
            Controls.Add(txtLog);

            Log("就绪。安装目录：" + root);
            if (!File.Exists(cliPy))
                Log("警告：没找到 netdev_cli.py —— 启动器可能不在安装根目录。");
        }

        Label AddRow(Panel card, int y, string name)
        {
            Label k = new Label();
            k.Text = name;
            k.ForeColor = Color.FromArgb(148, 163, 184);
            k.AutoSize = true;
            k.Location = new Point(20, y);
            card.Controls.Add(k);

            Label v = new Label();
            v.Text = "—";
            v.ForeColor = Color.FromArgb(51, 65, 85);
            v.AutoSize = true;
            v.Location = new Point(72, y);
            card.Controls.Add(v);
            return v;
        }

        Button AddButton(string text, int x, int y, int w, Color back, Color fore)
        {
            Button b = new Button();
            b.Text = text;
            b.Location = new Point(x, y);
            b.Size = new Size(w, 36);
            b.FlatStyle = FlatStyle.Flat;
            b.FlatAppearance.BorderSize = back == Color.White ? 1 : 0;
            b.FlatAppearance.BorderColor = Color.FromArgb(203, 213, 225);
            b.BackColor = back;
            b.ForeColor = fore;
            b.Font = new Font("Microsoft YaHei UI", 9.5F, FontStyle.Bold, GraphicsUnit.Point);
            b.Cursor = Cursors.Hand;
            b.UseVisualStyleBackColor = false;
            Controls.Add(b);
            return b;
        }

        string ReadVersion()
        {
            try
            {
                string f = Path.Combine(root, "VERSION");
                if (File.Exists(f))
                    return File.ReadAllText(f).Trim();
            }
            catch { }
            return "?";
        }

        // ─────────────────────────────────────────────── 状态自检
        void RefreshStatusAsync()
        {
            ThreadPool.QueueUserWorkItem(delegate
            {
                bool running = false;
                string detail = "";
                try
                {
                    running = HttpHealth(out detail);
                }
                catch (Exception ex) { detail = ex.GetType().Name; }
                int pid = ReadPid();
                bool auto = AutoStartInstalled();
                try
                {
                    BeginInvoke((MethodInvoker)delegate { ApplyStatus(running, detail, pid, auto); });
                }
                catch { }
            });
        }

        bool HttpHealth(out string detail)
        {
            detail = "";
            bool portOpen = false;
            try
            {
                using (TcpClient c = new TcpClient())
                {
                    IAsyncResult ar = c.BeginConnect(Host, Port, null, null);
                    if (!ar.AsyncWaitHandle.WaitOne(800))
                    {
                        detail = "端口未监听";
                        return false;
                    }
                    c.EndConnect(ar);
                    portOpen = true;
                }
            }
            catch (Exception ex)
            {
                detail = "端口未监听（" + ex.GetType().Name + "）";
                return false;
            }
            if (!portOpen)
            {
                detail = "端口未监听";
                return false;
            }
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create("http://" + Host + ":" + Port + "/api/health");
                req.Timeout = 2500;
                req.ReadWriteTimeout = 2500;
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                using (StreamReader sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                {
                    string body = sr.ReadToEnd();
                    if (resp.StatusCode == HttpStatusCode.OK && body.Replace("'", "\"").Contains("\"ok\": true"))
                    {
                        detail = "HTTP 200";
                        return true;
                    }
                    detail = "端口开着，但服务未正常应答";
                    return false;
                }
            }
            catch (Exception ex)
            {
                detail = "端口开着，但 /api/health 不通（" + ex.GetType().Name + "）";
                return false;
            }
        }

        int ReadPid()
        {
            try
            {
                if (File.Exists(pidFile))
                {
                    int p;
                    if (int.TryParse(File.ReadAllText(pidFile).Trim(), out p))
                        return p;
                }
            }
            catch { }
            return 0;
        }

        bool AutoStartInstalled()
        {
            try
            {
                string startup = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                    @"Microsoft\Windows\Start Menu\Programs\Startup\netdev-ui.lnk");
                if (File.Exists(startup)) return true;
                using (Microsoft.Win32.RegistryKey k = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(
                    @"Software\Microsoft\Windows\CurrentVersion\Run"))
                {
                    if (k != null && k.GetValue("netdev-ui") != null) return true;
                }
            }
            catch { }
            return false;
        }

        void ApplyStatus(bool running, string detail, int pid, bool auto)
        {
            lastRunning = running;
            if (running)
            {
                pnlDot.BackColor = Color.FromArgb(22, 163, 74);
                lblStatusText.Text = "服务运行中";
                lblStatusText.ForeColor = Color.FromArgb(21, 128, 61);
                lblStatusHint.Text = "网页界面已就绪，点「打开网页界面」即可使用";
                btnOpen.Enabled = true;
            }
            else
            {
                pnlDot.BackColor = Color.FromArgb(220, 38, 38);
                lblStatusText.Text = "服务未运行";
                lblStatusText.ForeColor = Color.FromArgb(185, 28, 28);
                lblStatusHint.Text = detail + " —— 点「启动 / 重启服务」，或「一键修复」";
                btnOpen.Enabled = true;   // 打开时会自动拉起
            }
            lblAddrV.Text = "http://" + Host + ":" + Port;
            lblPidV.Text = pid > 0 ? (pid + (running ? "（存活）" : "（PID 文件残留）")) : "无";
            lblVerV.Text = version;
            lblAutoV.Text = auto ? "已启用（登录后自动启动）" : "未启用";
            lblAutoV.ForeColor = auto ? Color.FromArgb(21, 128, 61) : Color.FromArgb(180, 83, 9);
        }

        // ─────────────────────────────────────────────── 动作
        void DoOpen()
        {
            SetBusy(true);
            Log("→ 确保网页服务在运行…");
            RunAsync(pyExe, Quote(cliPy) + " ui", delegate(int rc, string outp)
            {
                AppendOutput(outp);
                if (rc == 0)
                {
                    Log("→ 在浏览器打开 http://" + Host + ":" + Port);
                    try { Process.Start("http://" + Host + ":" + Port); }
                    catch (Exception ex) { Log("打开浏览器失败：" + ex.Message); }
                }
                else
                {
                    Log("服务未能启动（退出码 " + rc + "）。可点「一键修复」排查。");
                }
                SetBusy(false);
                RefreshStatusAsync();
            });
        }

        void DoRestart()
        {
            SetBusy(true);
            Log("→ 重启网页服务…");
            RunAsync(pyExe, Quote(cliPy) + " ui restart", delegate(int rc, string outp)
            {
                AppendOutput(outp);
                Log(rc == 0 ? "服务已就绪。" : "重启返回非零（" + rc + "），可试「一键修复」。");
                SetBusy(false);
                RefreshStatusAsync();
            });
        }

        void DoFix()
        {
            if (!File.Exists(healthPs1))
            {
                Log("找不到 一键体检.ps1 —— 无法修复。请重新安装或手动运行 netdev doctor。");
                return;
            }
            SetBusy(true);
            Log("→ 运行一键修复（体检 + 自动修复）…");
            string args = "-NoProfile -ExecutionPolicy Bypass -File " + Quote(healthPs1) + " -Fix -NoPause -Root " + Quote(root);
            RunAsync("powershell.exe", args, delegate(int rc, string outp)
            {
                AppendOutput(outp);
                Log(rc == 0 ? "修复完成，服务应已就绪。" : "修复结束（退出码 " + rc + "）—— 详见上方日志。");
                SetBusy(false);
                RefreshStatusAsync();
            });
        }

        void DoOpenLog()
        {
            try
            {
                if (!File.Exists(logFile))
                {
                    Log("暂无日志文件：" + logFile);
                    return;
                }
                Process.Start("notepad.exe", Quote(logFile));
            }
            catch (Exception ex) { Log("打开日志失败：" + ex.Message); }
        }

        void DoOpenDir()
        {
            try { Process.Start("explorer.exe", Quote(root)); }
            catch (Exception ex) { Log("打开目录失败：" + ex.Message); }
        }

        // ─────────────────────────────────────────────── 子进程与日志
        void RunAsync(string exe, string args, Action<int, string> done)
        {
            ThreadPool.QueueUserWorkItem(delegate
            {
                int rc = -1;
                string outp = "";
                try
                {
                    ProcessStartInfo psi = new ProcessStartInfo();
                    psi.FileName = exe;
                    psi.Arguments = args;
                    psi.WorkingDirectory = root;
                    psi.UseShellExecute = false;
                    psi.CreateNoWindow = true;
                    psi.RedirectStandardOutput = true;
                    psi.RedirectStandardError = true;
                    psi.StandardOutputEncoding = Encoding.UTF8;
                    psi.StandardErrorEncoding = Encoding.UTF8;
                    // ★ 2026-10-07：必须把子进程的输出编码钉成 UTF-8。
                    //   这里直接拉的是 venv 的 python.exe（不走 netdev.cmd），中文 Windows 下
                    //   它的 stdout 默认是 GBK；而本 CLI 会打印 ✔ / ✘ 这类非 GBK 字符，
                    //   一编码不上就 UnicodeEncodeError、退出码 1 —— 用户实测「点『启动/重启
                    //   服务』报 服务未能启动（退出码 1）」，日志里就是这行 traceback。
                    //   netdev.cmd / netdev-mcp.cmd 里设的也是这两个变量，行为保持一致。
                    psi.EnvironmentVariables["PYTHONUTF8"] = "1";
                    psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                    using (Process p = Process.Start(psi))
                    {
                        string o = p.StandardOutput.ReadToEnd();
                        string e = p.StandardError.ReadToEnd();
                        p.WaitForExit();
                        rc = p.ExitCode;
                        outp = (o + (string.IsNullOrEmpty(e) ? "" : "\n" + e)).Trim();
                    }
                }
                catch (Exception ex)
                {
                    outp = "启动失败：" + ex.Message;
                }
                try { BeginInvoke((MethodInvoker)delegate { done(rc, outp); }); }
                catch { }
            });
        }

        void SetBusy(bool b)
        {
            busy = b;
            btnOpen.Enabled = !b;
            btnRestart.Enabled = !b;
            btnFix.Enabled = !b;
            btnRefresh.Enabled = !b;
            Cursor = b ? Cursors.WaitCursor : Cursors.Default;
        }

        void AppendOutput(string s)
        {
            if (string.IsNullOrEmpty(s)) return;
            foreach (string line in s.Replace("\r\n", "\n").Split('\n'))
            {
                if (line.Trim().Length > 0) Log("  " + line);
            }
        }

        void Log(string s)
        {
            if (txtLog.InvokeRequired)
            {
                try { txtLog.BeginInvoke((MethodInvoker)delegate { Log(s); }); }
                catch { }
                return;
            }
            txtLog.AppendText("[" + DateTime.Now.ToString("HH:mm:ss") + "] " + s + "\r\n");
        }

        static string Quote(string s)
        {
            return "\"" + s + "\"";
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (busy)
            {
                if (MessageBox.Show("有操作正在进行，确定要关闭吗？", "netdev 工具台",
                        MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                {
                    e.Cancel = true;
                    return;
                }
            }
            base.OnFormClosing(e);
        }
    }
}
