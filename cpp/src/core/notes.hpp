// run/instance.json and run/boot.json: the server's two notes (6b310,
// 6b321). Step 1 reads them with a flat-object reader of its own (strings
// and integers, nothing nested) so the shell and the front carry no
// dependency at all; the engine (step 2) brings nlohmann::ordered_json.
#pragma once

#include <map>
#include <optional>
#include <string>

namespace concorde {

// A flat JSON object: every value kept as its decoded string (numbers as
// their digits). Nested values make the parse fail. Public for the fuzzer.
std::optional<std::map<std::string, std::string>> parse_flat_json(const std::string& s);

struct InstanceNote {
  int port = 0;
  std::string key, token;
  long pid = 0;
};
struct BootNote {
  std::string code;
  long pid = 0;
};

std::optional<InstanceNote> read_instance_note(const std::string& path);
std::optional<BootNote> read_boot_note(const std::string& path);

// The same bytes Python's json.dump writes (", " and ": "), key order kept.
std::string instance_note_json(const InstanceNote& n);
std::string boot_note_json(const BootNote& n);

}  // namespace concorde
