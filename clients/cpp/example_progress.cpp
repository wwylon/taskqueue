// Smoke: print progress and poll control once.
// Build: cl /EHsc /std:c++17 example_progress.cpp
//    or: g++ -std=c++17 example_progress.cpp -o example_progress

#include "tq_client.hpp"

int main() {
  tq::progress(10.0, "warmup");
  tq::maybe_suspend();
  tq::progress(100.0);
  return 0;
}
