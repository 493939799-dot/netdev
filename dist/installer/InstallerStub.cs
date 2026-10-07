// ═══════════════════════════════════════════════════════════════════════════
//  netdev 网络设备工具台 · Windows 图形安装向导（netdev-install.exe）
// ───────────────────────────────────────────────────────────────────────────
//  为什么改图形：原版是控制台 exe，双击会弹黑窗、中文还可能乱码，普通用户
//  看到一屏 PowerShell 输出会以为装坏了。这一版：
//    · /target:winexe —— 全程无控制台窗口；
//    · 可自选安装目录；可勾选开机自启 / 装完打开界面；
//    · 实时把 install.ps1 的输出滚进日志框，并按「N/8」步骤推进进度条；
//    · 结束时给出明确结论，失败时把日志留在窗口里供复制。
//
//  编译（csc.exe 来自 .NET Framework，Windows 自带）：
//    csc.exe /nologo /target:winexe /codepage:65001 ^
//            /reference:System.Windows.Forms.dll /reference:System.Drawing.dll ^
//            /win32icon:netdev.ico /out:netdev-install.exe InstallerStub.cs
// ═══════════════════════════════════════════════════════════════════════════
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

namespace NetdevInstaller
{
    static class Program
    {
        [STAThread]
        static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            string prefill = ParsePrefix(args);
            Application.Run(new InstallForm(prefill));
        }

        static string ParsePrefix(string[] args)
        {
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i];
                if ((a == "/Prefix" || a == "-Prefix" || a == "/prefix") && i + 1 < args.Length)
                    return args[i + 1];
            }
            return "";
        }
    }

    class InstallForm : Form
    {
        readonly string exeDir;
        readonly string psScript;

        TextBox txtPrefix;
        Button btnBrowse;
        CheckBox chkAutoStart, chkOpenUi;
        Button btnInstall;
        ProgressBar progress;
        Label lblStep;
        TextBox txtLog;
        bool running;
        bool succeeded;

        public InstallForm(string prefill)
        {
            exeDir = Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);
            psScript = Path.Combine(exeDir, "install.ps1");

            BuildUi();

            string def = string.IsNullOrEmpty(prefill)
                ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "netops")
                : prefill;
            txtPrefix.Text = def;

            Log("安装向导就绪。");
            Log("安装脚本：" + psScript);
            if (!File.Exists(psScript))
                Log("错误：本程序旁边找不到 install.ps1 —— 请把 zip 完整解压后再运行。");
        }

        void BuildUi()
        {
            Text = "netdev 网络设备工具台 · 安装向导";
            ClientSize = new Size(660, 620);
            MinimumSize = new Size(560, 520);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(244, 246, 250);
            Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular, GraphicsUnit.Point);
            AutoScaleMode = AutoScaleMode.Dpi;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }

            // ── 顶部横幅
            Panel header = new Panel();
            header.BackColor = Color.FromArgb(15, 23, 42);
            header.Dock = DockStyle.Top;
            header.Height = 76;
            Controls.Add(header);

            PictureBox pic = new PictureBox();
            pic.Location = new Point(18, 18);
            pic.Size = new Size(40, 40);
            pic.SizeMode = PictureBoxSizeMode.Zoom;
            pic.BackColor = Color.Transparent;
            try { pic.Image = Icon.ExtractAssociatedIcon(Application.ExecutablePath).ToBitmap(); } catch { }
            header.Controls.Add(pic);

            Label t = new Label();
            t.Text = "netdev 网络设备工具台 · 安装向导";
            t.ForeColor = Color.White;
            t.Font = new Font("Microsoft YaHei UI", 13F, FontStyle.Bold, GraphicsUnit.Point);
            t.AutoSize = true;
            t.BackColor = Color.Transparent;
            t.Location = new Point(70, 16);
            header.Controls.Add(t);

            Label s = new Label();
            s.Text = "把串口 / SSH / Telnet 接入的网络设备变成「人机同屏」会话";
            s.ForeColor = Color.FromArgb(148, 163, 184);
            s.AutoSize = true;
            s.BackColor = Color.Transparent;
            s.Location = new Point(72, 46);
            header.Controls.Add(s);

            // ── 安装目录
            Label l1 = new Label();
            l1.Text = "安装目录";
            l1.AutoSize = true;
            l1.Location = new Point(20, 96);
            Controls.Add(l1);

            txtPrefix = new TextBox();
            txtPrefix.Location = new Point(20, 118);
            txtPrefix.Size = new Size(500, 26);
            txtPrefix.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            Controls.Add(txtPrefix);

            btnBrowse = new Button();
            btnBrowse.Text = "浏览…";
            btnBrowse.Location = new Point(530, 117);
            btnBrowse.Size = new Size(110, 28);
            btnBrowse.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            btnBrowse.Click += delegate
            {
                using (FolderBrowserDialog d = new FolderBrowserDialog())
                {
                    d.Description = "选择 netdev 安装目录";
                    if (Directory.Exists(txtPrefix.Text)) d.SelectedPath = txtPrefix.Text;
                    if (d.ShowDialog() == DialogResult.OK) txtPrefix.Text = d.SelectedPath;
                }
            };
            Controls.Add(btnBrowse);

            chkAutoStart = new CheckBox();
            chkAutoStart.Text = "开机自动启动服务（登录后隐藏运行，推荐）";
            chkAutoStart.Checked = true;
            chkAutoStart.AutoSize = true;
            chkAutoStart.Location = new Point(20, 156);
            Controls.Add(chkAutoStart);

            chkOpenUi = new CheckBox();
            chkOpenUi.Text = "安装完成后打开网页界面";
            chkOpenUi.Checked = true;
            chkOpenUi.AutoSize = true;
            chkOpenUi.Location = new Point(20, 182);
            Controls.Add(chkOpenUi);

            Label note = new Label();
            note.Text = "提示：需要先装好 Python 3.10+（64 位，安装时勾选 Add to PATH）。"
                      + "安装过程会联网下载依赖，约 2~5 分钟。重复安装只更新程序，保留你的配置与备份。";
            note.ForeColor = Color.FromArgb(100, 116, 139);
            note.Location = new Point(20, 210);
            note.Size = new Size(620, 36);
            note.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            Controls.Add(note);

            btnInstall = new Button();
            btnInstall.Text = "开始安装";
            btnInstall.Location = new Point(20, 252);
            btnInstall.Size = new Size(160, 40);
            btnInstall.FlatStyle = FlatStyle.Flat;
            btnInstall.FlatAppearance.BorderSize = 0;
            btnInstall.BackColor = Color.FromArgb(22, 163, 74);
            btnInstall.ForeColor = Color.White;
            btnInstall.Font = new Font("Microsoft YaHei UI", 10.5F, FontStyle.Bold, GraphicsUnit.Point);
            btnInstall.Cursor = Cursors.Hand;
            btnInstall.UseVisualStyleBackColor = false;
            btnInstall.Click += delegate { OnInstallButton(); };
            Controls.Add(btnInstall);

            progress = new ProgressBar();
            progress.Location = new Point(196, 252);
            progress.Size = new Size(444, 18);
            progress.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            progress.Minimum = 0;
            progress.Maximum = 8;
            progress.Style = ProgressBarStyle.Continuous;
            Controls.Add(progress);

            lblStep = new Label();
            lblStep.Text = "等待开始…";
            lblStep.ForeColor = Color.FromArgb(100, 116, 139);
            lblStep.AutoSize = true;
            lblStep.Location = new Point(196, 276);
            lblStep.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            Controls.Add(lblStep);

            Label lc = new Label();
            lc.Text = "安装日志";
            lc.ForeColor = Color.FromArgb(100, 116, 139);
            lc.AutoSize = true;
            lc.Location = new Point(20, 306);
            Controls.Add(lc);

            txtLog = new TextBox();
            txtLog.Multiline = true;
            txtLog.ReadOnly = true;
            txtLog.ScrollBars = ScrollBars.Vertical;
            txtLog.BackColor = Color.FromArgb(15, 23, 42);
            txtLog.ForeColor = Color.FromArgb(203, 213, 225);
            txtLog.Font = new Font("Consolas", 8.5F, FontStyle.Regular, GraphicsUnit.Point);
            txtLog.BorderStyle = BorderStyle.FixedSingle;
            txtLog.Location = new Point(20, 328);
            txtLog.Size = new Size(620, 272);
            txtLog.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            Controls.Add(txtLog);
        }

        // ─────────────────────────────────────────────── 安装流程
        void OnInstallButton()
        {
            if (running) return;
            if (succeeded) { Close(); return; }
            StartInstall();
        }

        void StartInstall()
        {
            if (running) return;
            if (!File.Exists(psScript))
            {
                MessageBox.Show("找不到 install.ps1，请把安装包 zip 完整解压后再运行。",
                    "netdev 安装向导", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            string prefix = txtPrefix.Text.Trim();
            if (prefix.Length == 0)
            {
                MessageBox.Show("请填写安装目录。", "netdev 安装向导",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            running = true;
            succeeded = false;
            SetControlsEnabled(false);
            progress.Value = 0;
            lblStep.Text = "正在安装…";
            Log("");
            Log("════ 开始安装到 " + prefix + " ════");

            string psArgs = BuildArgs(prefix, !chkAutoStart.Checked);
            RunInstaller(psArgs, prefix);
        }

        string BuildArgs(string prefix, bool noAutoStart)
        {
            // 用 -Command 前置一行，把子 PowerShell 的输出编码切成 UTF-8，
            // 这样中文日志经管道回来不会乱码（PS 5.1 默认走 OEM 代码页）。
            string cmd = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                       + "& '" + psScript.Replace("'", "''") + "'"
                       + " -Prefix '" + prefix.Replace("'", "''") + "'";
            if (noAutoStart) cmd += " -NoAutoStart";
            return "-NoProfile -ExecutionPolicy Bypass -Command \"" + cmd + "\"";
        }

        void RunInstaller(string psArgs, string prefix)
        {
            Process p = new Process();
            p.StartInfo.FileName = "powershell.exe";
            p.StartInfo.Arguments = psArgs;
            p.StartInfo.WorkingDirectory = exeDir;
            p.StartInfo.UseShellExecute = false;
            p.StartInfo.CreateNoWindow = true;
            p.StartInfo.RedirectStandardOutput = true;
            p.StartInfo.RedirectStandardError = true;
            p.StartInfo.StandardOutputEncoding = Encoding.UTF8;
            p.StartInfo.StandardErrorEncoding = Encoding.UTF8;
            p.EnableRaisingEvents = true;
            p.OutputDataReceived += delegate(object s, DataReceivedEventArgs e)
            {
                if (e.Data != null) OnLine(e.Data);
            };
            p.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e)
            {
                if (e.Data != null) OnLine("! " + e.Data);
            };
            p.Exited += delegate { OnExited(p.ExitCode, prefix); };

            try
            {
                p.Start();
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
            }
            catch (Exception ex)
            {
                Log("无法启动安装脚本：" + ex.Message);
                Finish(false, prefix);
            }
        }

        void OnLine(string line)
        {
            if (InvokeRequired)
            {
                try { BeginInvoke((MethodInvoker)delegate { OnLine(line); }); } catch { }
                return;
            }
            Log(line);
            Match m = Regex.Match(line, @"==\s*(\d+)\s*/\s*8");
            if (m.Success)
            {
                int n;
                if (int.TryParse(m.Groups[1].Value, out n))
                {
                    if (n >= 1 && n <= 8 && n > progress.Value)
                    {
                        progress.Value = n;
                        lblStep.Text = "进行中：" + line.Trim().Trim('=', ' ').Trim();
                    }
                }
            }
        }

        void OnExited(int code, string prefix)
        {
            if (InvokeRequired)
            {
                try { BeginInvoke((MethodInvoker)delegate { OnExited(code, prefix); }); } catch { }
                return;
            }
            Finish(code == 0, prefix);
        }

        void Finish(bool ok, string prefix)
        {
            running = false;
            succeeded = ok;
            SetControlsEnabled(true);
            if (ok)
            {
                progress.Value = 8;
                lblStep.Text = "安装完成";
                Log("");
                Log("════ 安装完成 ════");
                Log("网页界面： http://127.0.0.1:8898");
                Log("下次可直接双击「netdev 工具台」快捷方式管理服务。");
                btnInstall.Text = "完成";
                if (chkOpenUi.Checked)
                {
                    try { Process.Start("http://127.0.0.1:8898"); } catch { }
                }
            }
            else
            {
                lblStep.Text = "安装失败";
                Log("");
                Log("════ 安装未完成 ════");
                Log("请把上方日志反馈给维护者；也可手动运行 一键体检.ps1 -Fix 排查。");
                btnInstall.Text = "重试";
            }
        }

        void SetControlsEnabled(bool b)
        {
            txtPrefix.Enabled = b;
            btnBrowse.Enabled = b;
            chkAutoStart.Enabled = b;
            chkOpenUi.Enabled = b;
            btnInstall.Enabled = b;
        }

        void Log(string s)
        {
            if (txtLog.InvokeRequired)
            {
                try { txtLog.BeginInvoke((MethodInvoker)delegate { Log(s); }); } catch { }
                return;
            }
            txtLog.AppendText(s + "\r\n");
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (running)
            {
                if (MessageBox.Show("安装正在进行，强行关闭可能留下不完整的安装。确定关闭吗？",
                        "netdev 安装向导", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes)
                {
                    e.Cancel = true;
                    return;
                }
            }
            base.OnFormClosing(e);
        }
    }
}
