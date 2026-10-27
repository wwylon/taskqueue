#pragma once

// Minimal taskqueue client helpers for C++ task scripts.
// Zero external dependencies — copy this single header into any project.
//
// Quick-start:
//   #include "tq_client.hpp"
//   tq::progress(50.0, "halfway");
//   tq::maybe_suspend();  // cooperative pause check
//
//   // W8.2: progress loop helper
//   std::vector<Item> items = ...;
//   for (auto& item : tq::progress_range(items, "processing")) { ... }
//
//   // Checkpoint cadence: work units OR ~10 min; reset both after save
//   tq::CheckpointCadence cadence(/*every_n_units=*/1000);
//   if (cadence.note_work()) { /* write checkpoint */ cadence.reset(); }
//
// Progress lines must match the daemon regex: \((\d+(?:\.\d+)?)%\)
// Cooperative suspend: exit with EXIT_SUSPENDED (75).

#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <initializer_list>
#include <iterator>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace tq {

inline constexpr int EXIT_SUSPENDED = 75;

// Fixed notation: the default stream format can print 1e-05, which the daemon regex does not match.
inline std::string format_pct(double pct) {
  if (!(pct >= 0.0)) pct = 0.0;  // also maps NaN to 0
  if (pct > 100.0) pct = 100.0;
  std::ostringstream ss;
  ss << std::fixed << std::setprecision(2) << pct;
  return ss.str();
}

inline void progress(double pct, std::string_view msg = {}) {
  if (!msg.empty()) {
    std::cout << msg << " (" << format_pct(pct) << "%)" << std::endl;
  } else {
    std::cout << "(" << format_pct(pct) << "%)" << std::endl;
  }
  std::cout.flush();
}

// Structured metrics: prints `TQ_METRIC {"loss":0.124,"epoch":3}`; the daemon merges keys into
// runs.metrics_json (last value per key wins). Keys are JSON-escaped; values are numbers.
inline void metric(std::initializer_list<std::pair<std::string_view, double>> fields) {
  if (fields.size() == 0) return;
  std::ostringstream ss;
  ss << "TQ_METRIC {";
  bool first = true;
  for (const auto& kv : fields) {
    if (!first) ss << ",";
    first = false;
    ss << '"';
    for (char c : kv.first) {
      if (c == '"' || c == '\\') ss << '\\' << c;
      else if (static_cast<unsigned char>(c) >= 0x20) ss << c;
    }
    ss << "\":" << std::setprecision(10) << kv.second;
  }
  ss << "}";
  std::cout << ss.str() << std::endl;
  std::cout.flush();
}

inline const char* env_or_null(const char* key) {
  return std::getenv(key);
}

inline std::optional<std::string> poll_control() {
  const char* path = std::getenv("TQ_CONTROL_FILE");
  if (!path || !*path) {
    return std::nullopt;
  }
  std::ifstream in(path);
  if (!in) {
    return std::nullopt;
  }
  std::ostringstream ss;
  ss << in.rdbuf();
  const std::string raw = ss.str();
  // Minimal JSON: look for "action" : "..."
  const std::string key = "\"action\"";
  const auto pos = raw.find(key);
  if (pos == std::string::npos) {
    return std::nullopt;
  }
  const auto colon = raw.find(':', pos + key.size());
  if (colon == std::string::npos) {
    return std::nullopt;
  }
  const auto q1 = raw.find('"', colon + 1);
  if (q1 == std::string::npos) {
    return std::nullopt;
  }
  const auto q2 = raw.find('"', q1 + 1);
  if (q2 == std::string::npos) {
    return std::nullopt;
  }
  return raw.substr(q1 + 1, q2 - q1 - 1);
}

[[noreturn]] inline void request_suspend_exit() {
  std::cout << "cooperative suspend requested; exiting " << EXIT_SUSPENDED << std::endl;
  std::cout.flush();
  std::exit(EXIT_SUSPENDED);
}

inline void maybe_suspend() {
  auto action = poll_control();
  if (action && *action == "suspend") {
    request_suspend_exit();
  }
}

// Dual-trigger checkpoint gate: work units OR wall time since last reset.
// Call save then reset() when due. Force-saves (suspend / SIGTERM) always write,
// then reset() — they do not go through due().
class CheckpointCadence {
 public:
  explicit CheckpointCadence(std::size_t every_n_units,
                             double every_sec = 600.0)
      : every_n_units_(every_n_units), every_sec_(every_sec), units_(0),
        t0_(std::chrono::steady_clock::now()) {
    if (every_n_units_ < 1) {
      throw std::invalid_argument("every_n_units must be >= 1");
    }
    if (every_sec_ <= 0.0) {
      throw std::invalid_argument("every_sec must be > 0");
    }
  }

  // Add work units and return whether a checkpoint is due (does not reset).
  bool note_work(std::size_t n = 1) {
    units_ += n;
    return due();
  }

  // True if work or time threshold is met since last reset().
  bool due() const {
    if (units_ >= every_n_units_) {
      return true;
    }
    const auto elapsed = std::chrono::steady_clock::now() - t0_;
    const double sec =
        std::chrono::duration<double>(elapsed).count();
    return sec >= every_sec_;
  }

  // Clear work counter and restart the timer (call after a successful save).
  void reset() {
    units_ = 0;
    t0_ = std::chrono::steady_clock::now();
  }

 private:
  std::size_t every_n_units_;
  double every_sec_;
  std::size_t units_;
  std::chrono::steady_clock::time_point t0_;
};

// W8.2: progress_range — iterate a container while reporting progress each step.
// Usage: for (auto& x : tq::progress_range(vec, "training")) { ... }
template <typename Container>
struct ProgressRange {
  const Container& items;
  std::string msg;

  struct Iterator {
    using InnerIt = decltype(std::begin(std::declval<const Container&>()));
    InnerIt it, end;
    std::size_t idx, total;
    std::string msg;
    std::size_t suspend_every;  // check maybe_suspend() every N increments

    Iterator& operator++() {
      ++it; ++idx;
      if (it != end) {
        double pct = total > 0 ? (idx * 100.0 / total) : 0.0;
        if (!msg.empty()) std::cout << msg << " (" << format_pct(pct) << "%)" << std::endl;
        else              std::cout << "(" << format_pct(pct) << "%)" << std::endl;
        if (suspend_every > 0 && idx % suspend_every == 0) maybe_suspend();
      } else {
        if (!msg.empty()) std::cout << msg << " (100%)" << std::endl;
        else              std::cout << "(100%)" << std::endl;
        maybe_suspend();
      }
      return *this;
    }
    auto& operator*() const { return *it; }
    bool operator!=(const Iterator& o) const { return it != o.it; }
  };

  Iterator begin() const {
    std::size_t n = static_cast<std::size_t>(std::distance(std::begin(items), std::end(items)));
    auto it = std::begin(items);
    if (it != std::end(items)) {
      if (!msg.empty()) std::cout << msg << " (0%)" << std::endl;
      else              std::cout << "(0%)" << std::endl;
      maybe_suspend();
    }
    return {it, std::end(items), 0, n, msg, suspend_every};
  }
  Iterator end() const {
    std::size_t n = static_cast<std::size_t>(std::distance(std::begin(items), std::end(items)));
    return {std::end(items), std::end(items), n, n, msg, suspend_every};
  }

  std::size_t suspend_every{100};
};

template <typename Container>
ProgressRange<Container> progress_range(const Container& c, std::string_view msg = {},
                                        std::size_t suspend_every = 100) {
  return {c, std::string(msg), suspend_every};
}

// ProgressRange keeps a reference: a temporary container would dangle, so rvalues are rejected.
template <typename Container>
void progress_range(const Container&& c, std::string_view msg = {},
                    std::size_t suspend_every = 100) = delete;

}  // namespace tq
