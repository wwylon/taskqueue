// Smoke: CheckpointCadence + save/load_checkpoint.
#include "tq_client.hpp"

#include <iostream>
#include <string>

int main() {
  const std::string path = "example_checkpoint.json";
  tq::CheckpointCadence cadence(/*every_n_units=*/2, /*every_sec=*/3600.0);
  for (int i = 0; i < 3; ++i) {
    tq::progress(100.0 * (i + 1) / 3.0, "ckpt-demo");
    if (cadence.note_work()) {
      tq::save_checkpoint(path, std::string("{\"step\":") + std::to_string(i + 1) + "}");
      cadence.reset();
    }
  }
  auto loaded = tq::load_checkpoint(path);
  if (!loaded) {
    std::cerr << "load_checkpoint failed\n";
    return 1;
  }
  std::cout << "loaded " << *loaded << std::endl;
  return 0;
}
