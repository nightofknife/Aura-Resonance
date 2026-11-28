#include <algorithm>
#include <array>
#include <cstdint>
#include <memory>
#include <new>
#include <vector>

namespace {
struct Batch {
    int bucket;
    int begin;
    int end;
    std::array<int, 3> count{};
    std::array<int, 3> write{};
};
}

extern "C" __declspec(dllexport) int canonical_index(
    const std::int32_t* representatives, const std::int32_t* first, const std::int32_t* second,
    const std::int32_t* group, const std::int32_t* permutation_index,
    int buckets, int slots, int placements, int symmetry_count, int identity, int workers,
    std::int32_t* players, std::int32_t* bosses, std::int32_t* first_slots, std::int32_t* second_slots,
    std::int8_t* free_count, std::int32_t* ids
) try {
    const int ranks = slots * placements;
    const int total_ranks = buckets * ranks;
    std::unique_ptr<int[]> canonical(new int[total_ranks]);
    std::unique_ptr<int[]> rank_to_id(new int[total_ranks]);
    std::vector<std::vector<int>> stabilizers(buckets);
    for (int bucket = 0; bucket < buckets; ++bucket) {
        const int p = representatives[bucket];
        for (int frame = 0; frame < symmetry_count; ++frame) {
            if (frame != identity && group[frame * (slots + 1) + p] == p) {
                stabilizers[bucket].push_back(frame);
            }
        }
    }
    std::vector<Batch> batches;
    for (int bucket = 0; bucket < buckets; ++bucket) {
        for (int start = 0; start < slots; start += 6) {
            batches.push_back({bucket, start, std::min(start + 6, slots)});
        }
    }
    const int work = int(batches.size());
    int total = 0;
    #pragma omp parallel num_threads(workers) if(workers > 1)
    {
        #pragma omp for schedule(static)
        for (int task = 0; task < work; ++task) {
            Batch& batch = batches[task];
            const int bucket = batch.bucket;
            const int p = representatives[bucket];
            int* values = canonical.get() + bucket * ranks;
            for (int b = batch.begin; b < batch.end; ++b) {
                for (int item = 0; item < placements; ++item) {
                    const int rank = b * placements + item;
                    const int i = first[item];
                    const int j = second[item];
                    if (p == b || p == i || p == j || b == i || b == j) {
                        values[rank] = -1;
                        continue;
                    }
                    int best = rank;
                    for (int frame : stabilizers[bucket]) {
                        const int transformed_boss = group[frame * (slots + 1) + b];
                        const int transformed_item = permutation_index[frame * placements + item];
                        best = std::min(best, transformed_boss * placements + transformed_item);
                    }
                    values[rank] = best;
                    if (best == rank) ++batch.count[(i != slots) + (j != slots)];
                }
            }
        }
        #pragma omp single
        {
            for (int num_free = 0; num_free < 3; ++num_free) {
                for (Batch& batch : batches) {
                    batch.write[num_free] = total;
                    total += batch.count[num_free];
                }
            }
        }
        #pragma omp for schedule(static)
        for (int task = 0; task < work; ++task) {
            Batch& batch = batches[task];
            const int bucket = batch.bucket;
            const int p = representatives[bucket];
            const int* values = canonical.get() + bucket * ranks;
            int* output = rank_to_id.get() + bucket * ranks;
            auto write = batch.write;
            for (int b = batch.begin; b < batch.end; ++b) {
                for (int item = 0; item < placements; ++item) {
                    const int rank = b * placements + item;
                    if (values[rank] != rank) continue;
                    const int i = first[item];
                    const int j = second[item];
                    const int free = (i != slots) + (j != slots);
                    const int id = write[free]++;
                    output[rank] = id;
                    players[id] = p;
                    bosses[id] = b;
                    first_slots[id] = i;
                    second_slots[id] = j;
                    free_count[id] = std::int8_t(free);
                }
            }
        }
        #pragma omp for schedule(static)
        for (int task = 0; task < work; ++task) {
            const Batch& batch = batches[task];
            const int bucket = batch.bucket;
            const int* values = canonical.get() + bucket * ranks;
            const int* canonical_ids = rank_to_id.get() + bucket * ranks;
            for (int b = batch.begin; b < batch.end; ++b) {
                auto* dest = ids + (bucket * slots + b) * (placements + 1);
                for (int item = 0; item < placements; ++item) {
                    const int rank = values[b * placements + item];
                    dest[item] = rank < 0 ? -1 : canonical_ids[rank];
                }
                dest[placements] = -1;
            }
        }
    }
    return total;
} catch (const std::bad_alloc&) {
    return -1;
}
