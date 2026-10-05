//! Keep the sidecar from outliving this host on Windows.
//!
//! The sidecar is a child process, and a child process is not tied to its parent's
//! lifetime.  This host usually ends that lifetime itself -- a tray Quit calls
//! `bridge.stop()`, which asks the sidecar to shut down and then waits for it -- but a
//! host that is *terminated* runs none of that.  Task Manager's "End task", a crash, or
//! an installer replacing the executable all end the host without a chance to clean up,
//! and the child keeps running.
//!
//! That would be a stray process, except for where the sidecar runs from.  In the
//! directory shape it executes *out of the install directory*, and Windows keeps a
//! running process's image and every DLL it loaded locked: 191 files under
//! `sidecar/`.  An orphan holds all of them, so the next install or upgrade fails with
//! "error opening file for writing" -- and the failure outlives a reboot only until the
//! next install, which is to say it recurs.
//!
//! A Windows **job object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is the OS-level
//! answer: every process in the job is terminated when the job's last handle closes, and
//! that happens when this process dies, however it dies.  Nothing has to run for it, which
//! is the point -- the failure mode is "nothing ran".
//!
//! # Why this is allowed to fail
//!
//! Assigning to a job can be refused (a host already inside a restrictive job of its own,
//! on a Windows older than 8 where jobs did not nest).  Every such case is treated as
//! "this protection is unavailable" rather than an error: the sidecar still starts, and
//! the host still stops it on every path that can, which is the behaviour before this
//! module existed.  A launch must not fail because a hardening step was declined.

/// Holds the sidecar for as long as this host lives.
///
/// Returned by [`confine`], and intentionally never read: its value is its `Drop`, which
/// closes the job's only handle and so terminates the process.  Callers keep it alive for
/// as long as they want the child to be tied to them.
#[cfg(windows)]
#[derive(Debug)]
pub struct Confined {
    /// The job's handle, as an integer.
    ///
    /// Not a `HANDLE`, because that wraps a raw pointer and so is neither `Send` nor
    /// `Sync` -- and the bridge that owns this lives in Tauri's managed state, which
    /// requires both.  It is closed in `Drop` below.
    job: isize,
}

// The handle is an opaque kernel value: a process-wide job object, only ever passed back
// to the API that closes it.  Nothing here reads through it, so sharing it across threads
// cannot observe a partial write, which is the property `Send`/`Sync` are about.
#[cfg(windows)]
unsafe impl Send for Confined {}
#[cfg(windows)]
unsafe impl Sync for Confined {}

#[cfg(windows)]
impl Drop for Confined {
    fn drop(&mut self) {
        unsafe {
            let _ = windows::Win32::Foundation::CloseHandle(
                windows::Win32::Foundation::HANDLE(self.job as *mut std::ffi::c_void),
            );
        }
    }
}

/// The limits that make the job end its processes when the last handle closes.
///
/// Split out from [`confine`] so the one value that makes this work can be asserted
/// directly: a wrong flag still returns `Ok` from the calls below and still looks
/// confined, and the only other way to notice is a sidecar that survives a force-kill.
#[cfg(windows)]
fn kill_on_close_limits() -> windows::Win32::System::JobObjects::JOBOBJECT_EXTENDED_LIMIT_INFORMATION
{
    use windows::Win32::System::JobObjects::{
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    limits
}

/// Tie `process` to this host's lifetime, if the OS will let us.
///
/// `process` is the raw handle from spawning the sidecar.  On success the returned
/// [`Confined`] must be kept alive for as long as the child should live; dropping it ends
/// the child.  On failure the child is unaffected and the caller should log and carry on.
#[cfg(windows)]
pub fn confine(process: Option<isize>) -> Result<Confined, String> {
    // `None` means the child already exited, which is not a failure: there is nothing
    // left to tie to this process, and nothing to clean up.
    let Some(process) = process else {
        return Err("the sidecar had already exited".to_string());
    };
    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    };

    unsafe {
        let job: HANDLE =
            CreateJobObjectW(None, None).map_err(|error| format!("CreateJobObject: {error}"))?;
        let limits = kill_on_close_limits();
        // `size_of` rather than the struct's own declared size: the API wants the size of
        // the structure being passed, and a mismatch is how this call fails silently.
        let size = std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32;
        if let Err(error) = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &limits as *const _ as *const std::ffi::c_void,
            size,
        ) {
            let _ = windows::Win32::Foundation::CloseHandle(job);
            return Err(format!("SetInformationJobObject: {error}"));
        }
        if let Err(error) = AssignProcessToJobObject(job, HANDLE(process as *mut std::ffi::c_void)) {
            let _ = windows::Win32::Foundation::CloseHandle(job);
            return Err(format!("AssignProcessToJobObject: {error}"));
        }
        Ok(Confined { job: job.0 as isize })
    }
}

/// Not Windows: there is nothing to do, so the call site needs no `cfg` of its own.
#[cfg(not(windows))]
#[derive(Debug)]
pub struct Confined;

/// See the Windows definition above.
#[cfg(not(windows))]
pub fn confine(_process: Option<isize>) -> Result<Confined, String> {
    Ok(Confined)
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

    /// Whether a pid is still running, by asking for a handle to it.
    fn running(pid: u32) -> bool {
        use windows::Win32::System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        const STILL_ACTIVE: u32 = 259;
        unsafe {
            let Ok(handle) = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) else {
                return false;
            };
            let mut code = 0u32;
            let alive = GetExitCodeProcess(handle, &mut code).is_ok() && code == STILL_ACTIVE;
            let _ = windows::Win32::Foundation::CloseHandle(handle);
            alive
        }
    }

    /// A child that will not exit on its own, so only the job can end it.
    fn spawn_ping() -> std::process::Child {
        std::process::Command::new("cmd")
            .args(["/C", "ping -n 60 127.0.0.1"])
            .stdout(std::process::Stdio::null())
            .spawn()
            .expect("spawn a sleeper")
    }

    /// The whole point: closing the job ends a child that would otherwise keep running.
    ///
    /// Asserted by observing the process, not by trusting the return value: a version of
    /// `confine` that set the flag wrongly still returns `Ok`, and the failure is only
    /// visible in whether the process is alive afterwards.
    #[test]
    fn closing_the_job_ends_the_child() {
        let mut child = spawn_ping();
        let pid = child.id();
        assert!(running(pid), "the child should be running to begin with");
        use std::os::windows::io::AsRawHandle;
        let confined =
            confine(Some(child.as_raw_handle() as isize)).expect("confine the child");
        // Still alive: the job holds it, it does not end it early.
        std::thread::sleep(std::time::Duration::from_millis(200));
        assert!(running(pid), "confining must not end the child");

        drop(confined);
        // Termination is asynchronous; give the kernel a moment to reap it.
        let mut ended = false;
        for _ in 0..50 {
            if !running(pid) {
                ended = true;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        assert!(ended, "closing the job should have ended pid {pid}");
        let _ = child.wait();
    }

    /// The flag that makes the job kill its processes is the one we think it is.
    ///
    /// Cheap and exact, and it covers the failure the live test cannot: `confine` sets a
    /// flag, and a wrong flag is invisible in its return value.
    #[test]
    fn the_job_is_configured_to_kill_on_close() {
        use windows::Win32::System::JobObjects::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

        let limits = kill_on_close_limits();
        assert_eq!(
            limits.BasicLimitInformation.LimitFlags,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        );
    }
}
