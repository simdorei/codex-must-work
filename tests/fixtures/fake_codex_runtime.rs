use std::{env, fs, process};

fn json_escape(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

fn configured_source() -> Result<String, ()> {
    let home = env::var("CODEX_HOME").map_err(|_| ())?;
    let config = fs::read_to_string(format!("{home}/config.toml")).map_err(|_| ())?;
    for line in config.lines() {
        let trimmed = line.trim();
        if let Some(raw) = trimmed.strip_prefix("source = \"") {
            if let Some(encoded) = raw.strip_suffix('"') {
                return Ok(encoded.replace("\\\\", "\\").replace("\\\"", "\""));
            }
        }
    }
    Err(())
}

fn run() -> Result<(), ()> {
    let arguments: Vec<String> = env::args().skip(1).collect();
    match arguments.as_slice() {
        [flag] if flag == "--version" => println!("codex-cli 0.144.0"),
        [first, second] if first == "features" && second == "list" => {
            println!("hooks stable true");
            println!("plugins experimental true");
        }
        [first, second, third, fourth, fifth, marketplace]
            if first == "plugin"
                && second == "list"
                && third == "--available"
                && fourth == "--json"
                && fifth == "--marketplace"
                && marketplace == "simdorei" =>
        {
            let source = json_escape(&configured_source()?);
            println!(
                "{{\"installed\":[],\"available\":[{{\"pluginId\":\"codex-must-work@simdorei\",\
                 \"name\":\"codex-must-work\",\"marketplaceName\":\"simdorei\",\
                 \"source\":{{\"source\":\"local\",\"path\":\"{source}\"}}}}]}}"
            );
        }
        _ => return Err(()),
    }
    Ok(())
}

fn main() {
    if run().is_err() {
        process::exit(2);
    }
}
