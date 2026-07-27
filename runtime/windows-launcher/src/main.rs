//! Launch the bundled CMW Python runtime without a resident PowerShell parent.

use std::env;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};

const PLUGIN_NAME: &str = "codex-must-work";
const PYTHON_VERSION: &str = "3.12.13+20260510";

fn main() {
    let arguments = env::args_os().skip(1).collect::<Vec<_>>();
    let exit_code = match run(&arguments) {
        Ok(code) => code,
        Err(message) => {
            report_error(&message);
            1
        }
    };
    exit_process(exit_code);
}

fn run(arguments: &[OsString]) -> Result<i32, String> {
    let executable =
        env::current_exe().map_err(|error| format!("launcher path is unavailable: {error}"))?;
    let plugin_root = plugin_root(&executable)?;
    let data_root = plugin_data_root(&plugin_root)?;
    let python = ensure_python(&plugin_root, &data_root)?;
    let status = Command::new(&python)
        .arg("-B")
        .args(arguments)
        .env("PLUGIN_DATA", &data_root)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status()
        .map_err(|error| format!("portable Python did not start: {error}"))?;
    Ok(status_code(status))
}

fn plugin_root(executable: &Path) -> Result<PathBuf, String> {
    executable
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .ok_or_else(|| "launcher is not inside a plugin runtime directory".to_owned())
}

fn plugin_data_root(plugin_root: &Path) -> Result<PathBuf, String> {
    if let Some(configured) = env::var_os("PLUGIN_DATA").filter(|value| !value.is_empty()) {
        let path = PathBuf::from(configured);
        return path
            .is_absolute()
            .then_some(path)
            .ok_or_else(|| "PLUGIN_DATA must be absolute".to_owned());
    }
    let plugin_dir = parent(plugin_root, "plugin version")?;
    let marketplace_dir = parent(plugin_dir, "plugin name")?;
    let cache_dir = parent(marketplace_dir, "marketplace")?;
    let plugins_base = parent(cache_dir, "cache")?;
    if plugin_dir.file_name().and_then(|value| value.to_str()) != Some(PLUGIN_NAME)
        || cache_dir.file_name().and_then(|value| value.to_str()) != Some("cache")
    {
        return Err("PLUGIN_DATA is required outside an installed plugin cache".to_owned());
    }
    let marketplace = marketplace_dir
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "installed plugin marketplace is invalid".to_owned())?;
    Ok(plugins_base
        .join("data")
        .join(format!("{PLUGIN_NAME}-{marketplace}")))
}

fn parent<'a>(path: &'a Path, label: &str) -> Result<&'a Path, String> {
    path.parent()
        .ok_or_else(|| format!("installed plugin {label} path is invalid"))
}

fn ensure_python(plugin_root: &Path, data_root: &Path) -> Result<PathBuf, String> {
    if let Some(python) = ready_python(data_root) {
        return Ok(python);
    }
    prepare_python(plugin_root, data_root)?;
    ready_python(data_root).ok_or_else(|| "portable Python preparation was incomplete".to_owned())
}

fn ready_python(data_root: &Path) -> Option<PathBuf> {
    let prepared = data_root
        .join(format!("portable-python-{PYTHON_VERSION}"))
        .join("python.exe");
    if prepared.is_file() {
        return Some(prepared);
    }
    let bootstrapped = data_root
        .join("portable-python")
        .join(PYTHON_VERSION)
        .join("windows-x64")
        .join("python")
        .join("python.exe");
    bootstrapped.is_file().then_some(bootstrapped)
}

fn prepare_python(plugin_root: &Path, data_root: &Path) -> Result<(), String> {
    let system_root =
        env::var_os("SYSTEMROOT").ok_or_else(|| "SYSTEMROOT is unavailable".to_owned())?;
    let powershell = PathBuf::from(system_root)
        .join("System32")
        .join("WindowsPowerShell")
        .join("v1.0")
        .join("powershell.exe");
    let script = plugin_root.join("runtime").join("launch-python.ps1");
    let status = Command::new(&powershell)
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ])
        .arg(script)
        .arg("-PrepareOnly")
        .env("PLUGIN_DATA", data_root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .status()
        .map_err(|error| format!("portable Python preparation did not start: {error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!(
            "portable Python preparation failed with exit code {}",
            status_code(status)
        ))
    }
}

fn status_code(status: ExitStatus) -> i32 {
    status.code().unwrap_or(1)
}

#[allow(
    clippy::print_stderr,
    reason = "launcher failures must reach the MCP host"
)]
fn report_error(message: &str) {
    eprintln!("codex-must-work launcher: {message}");
}

#[allow(
    clippy::exit,
    reason = "the launcher must preserve the child process exit code"
)]
fn exit_process(code: i32) -> ! {
    std::process::exit(code);
}
