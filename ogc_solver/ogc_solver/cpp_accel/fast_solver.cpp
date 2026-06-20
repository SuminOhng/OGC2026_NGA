#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstdint>
#include <functional>
#include <iostream>
#include <limits>
#include <set>
#include <string>
#include <thread>
#include <tuple>
#include <vector>

namespace {

struct BBox {
    double min_x = 0.0;
    double min_y = 0.0;
    double max_x = 1.0;
    double max_y = 1.0;
};

struct Point {
    double x = 0.0;
    double y = 0.0;
};

struct Bay {
    double width = 0.0;
    double height = 0.0;
};

struct Block {
    int release = 0;
    int due = 0;
    int processing = 0;
    double workload = 0.0;
    std::vector<int> preferences;
    std::vector<BBox> boxes;
    std::vector<std::vector<BBox>> layer_boxes;
    std::vector<std::vector<std::vector<Point>>> layer_polygons;
};

struct Placement {
    int block_id = -1;
    int bay_id = 0;
    int orient_idx = 0;
    int x = 0;
    int y = 0;
    int entry = 0;
    int exit = 0;
    double workload = 0.0;
};

struct Candidate {
    bool ok = false;
    double score = std::numeric_limits<double>::infinity();
    Placement placement;
};

struct Problem {
    int time_ms = 1;
    std::vector<Bay> bays;
    std::vector<Block> blocks;
    std::vector<std::vector<Placement>> initial_seeds;
    double w1 = 1.0;
    double w2 = 1.0;
    double w3 = 1.0;
};

int g_overlap_mode = 0;
bool g_filter_feasible_like = false;
bool g_use_official_access = false;
double g_due_spacing_weight = 0.0;

class Timer {
public:
    explicit Timer(int time_ms)
        : start_(std::chrono::steady_clock::now()), limit_ms_(std::max(1, time_ms)) {}

    bool expired(int reserve_ms = 10) const {
        const auto now = std::chrono::steady_clock::now();
        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - start_).count();
        return elapsed + reserve_ms >= limit_ms_;
    }

    int remaining_ms() const {
        const auto now = std::chrono::steady_clock::now();
        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - start_).count();
        return std::max<int>(0, limit_ms_ - static_cast<int>(elapsed));
    }

private:
    std::chrono::steady_clock::time_point start_;
    int limit_ms_;
};

class Rng {
public:
    explicit Rng(uint64_t seed) : state_(seed ? seed : 1) {}

    int next_int(int limit) {
        if (limit <= 1) {
            return 0;
        }
        state_ = state_ * 6364136223846793005ULL + 1442695040888963407ULL;
        return static_cast<int>((state_ >> 32) % static_cast<uint64_t>(limit));
    }

    double next_double() {
        state_ = state_ * 6364136223846793005ULL + 1442695040888963407ULL;
        return static_cast<double>(state_ >> 11) * (1.0 / 9007199254740992.0);
    }

private:
    uint64_t state_;
};

bool read_problem(Problem& problem) {
    std::string magic;
    int bay_count = 0;
    int block_count = 0;
    if (!(std::cin >> magic) || magic != "OGC_FAST_SOLVER_V1") {
        return false;
    }
    if (!(std::cin >> problem.time_ms >> bay_count >> block_count)) {
        return false;
    }
    if (!(std::cin >> problem.w1 >> problem.w2 >> problem.w3)) {
        return false;
    }
    if (bay_count <= 0 || block_count <= 0) {
        return false;
    }

    problem.bays.resize(bay_count);
    for (int i = 0; i < bay_count; ++i) {
        if (!(std::cin >> problem.bays[i].width >> problem.bays[i].height)) {
            return false;
        }
    }

    problem.blocks.resize(block_count);
    for (int block_id = 0; block_id < block_count; ++block_id) {
        Block& block = problem.blocks[block_id];
        int orient_count = 0;
        if (!(std::cin >> block.release >> block.due >> block.processing >> block.workload)) {
            return false;
        }
        block.preferences.resize(bay_count);
        for (int bay_id = 0; bay_id < bay_count; ++bay_id) {
            if (!(std::cin >> block.preferences[bay_id])) {
                return false;
            }
        }
        if (!(std::cin >> orient_count) || orient_count <= 0) {
            return false;
        }
        block.boxes.resize(orient_count);
        block.layer_boxes.resize(orient_count);
        block.layer_polygons.resize(orient_count);
        for (int orient_idx = 0; orient_idx < orient_count; ++orient_idx) {
            BBox& box = block.boxes[orient_idx];
            if (!(std::cin >> box.min_x >> box.min_y >> box.max_x >> box.max_y)) {
                return false;
            }
            int layer_count = 0;
            if (!(std::cin >> layer_count) || layer_count < 0) {
                return false;
            }
            block.layer_boxes[orient_idx].resize(layer_count);
            block.layer_polygons[orient_idx].resize(layer_count);
            for (int layer_idx = 0; layer_idx < layer_count; ++layer_idx) {
                BBox& layer = block.layer_boxes[orient_idx][layer_idx];
                if (!(std::cin >> layer.min_x >> layer.min_y >> layer.max_x >> layer.max_y)) {
                    return false;
                }
                int vertex_count = 0;
                if (!(std::cin >> vertex_count) || vertex_count < 0) {
                    return false;
                }
                block.layer_polygons[orient_idx][layer_idx].resize(vertex_count);
                for (int vertex_idx = 0; vertex_idx < vertex_count; ++vertex_idx) {
                    Point& point = block.layer_polygons[orient_idx][layer_idx][vertex_idx];
                    if (!(std::cin >> point.x >> point.y)) {
                        return false;
                    }
                }
            }
        }
    }

    std::string seed_marker;
    if (std::cin >> seed_marker) {
        if (seed_marker == "SEEDS") {
            int seed_count = 0;
            if (!(std::cin >> seed_count) || seed_count < 0) {
                return false;
            }
            problem.initial_seeds.clear();
            problem.initial_seeds.reserve(seed_count);
            for (int seed_idx = 0; seed_idx < seed_count; ++seed_idx) {
                std::vector<Placement> seed;
                seed.reserve(block_count);
                for (int row = 0; row < block_count; ++row) {
                    Placement placement;
                    if (!(std::cin >> placement.block_id >> placement.bay_id >> placement.x >> placement.y >>
                          placement.orient_idx >> placement.entry >> placement.exit)) {
                        return false;
                    }
                    if (placement.block_id < 0 || placement.block_id >= block_count) {
                        return false;
                    }
                    placement.workload = problem.blocks[placement.block_id].workload;
                    seed.push_back(placement);
                }
                std::sort(seed.begin(), seed.end(), [](const Placement& a, const Placement& b) {
                    return a.block_id < b.block_id;
                });
                problem.initial_seeds.push_back(std::move(seed));
            }
        }
    }
    return true;
}

int lower_x(const BBox& box) {
    return std::max(0, static_cast<int>(std::ceil(-box.min_x - 1e-9)));
}

int lower_y(const BBox& box) {
    return std::max(0, static_cast<int>(std::ceil(-box.min_y - 1e-9)));
}

double rect_min_x(const Placement& placement, const Problem& problem) {
    return placement.x + problem.blocks[placement.block_id].boxes[placement.orient_idx].min_x;
}

double rect_min_y(const Placement& placement, const Problem& problem) {
    return placement.y + problem.blocks[placement.block_id].boxes[placement.orient_idx].min_y;
}

double rect_max_x(const Placement& placement, const Problem& problem) {
    return placement.x + problem.blocks[placement.block_id].boxes[placement.orient_idx].max_x;
}

double rect_max_y(const Placement& placement, const Problem& problem) {
    return placement.y + problem.blocks[placement.block_id].boxes[placement.orient_idx].max_y;
}

bool fits(const Bay& bay, const BBox& box, int x, int y) {
    return x + box.min_x >= -1e-7 && y + box.min_y >= -1e-7 &&
           x + box.max_x <= bay.width + 1e-7 && y + box.max_y <= bay.height + 1e-7;
}

bool overlap_rects(
    double ax1,
    double ay1,
    double ax2,
    double ay2,
    double bx1,
    double by1,
    double bx2,
    double by2
) {
    return ax1 < bx2 - 1e-7 && bx1 < ax2 - 1e-7 && ay1 < by2 - 1e-7 && by1 < ay2 - 1e-7;
}

double cross(const Point& a, const Point& b, const Point& c) {
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
}

Point shifted(const Point& p, int x, int y) {
    return Point{p.x + x, p.y + y};
}

bool proper_segment_intersection(const Point& a, const Point& b, const Point& c, const Point& d) {
    constexpr double eps = 1e-9;
    const double c1 = cross(a, b, c);
    const double c2 = cross(a, b, d);
    const double c3 = cross(c, d, a);
    const double c4 = cross(c, d, b);
    return (c1 > eps && c2 < -eps || c1 < -eps && c2 > eps) &&
           (c3 > eps && c4 < -eps || c3 < -eps && c4 > eps);
}

bool strictly_inside_polygon(const std::vector<Point>& polygon, const Point& point) {
    if (polygon.size() < 3) {
        return false;
    }
    bool inside = false;
    constexpr double eps = 1e-9;
    for (size_t i = 0, j = polygon.size() - 1; i < polygon.size(); j = i++) {
        const Point& a = polygon[i];
        const Point& b = polygon[j];
        if (std::abs(cross(a, b, point)) <= eps &&
            point.x >= std::min(a.x, b.x) - eps &&
            point.x <= std::max(a.x, b.x) + eps &&
            point.y >= std::min(a.y, b.y) - eps &&
            point.y <= std::max(a.y, b.y) + eps) {
            return false;
        }
        const bool crosses = ((a.y > point.y) != (b.y > point.y));
        if (crosses) {
            const double x_intersect = (b.x - a.x) * (point.y - a.y) / (b.y - a.y) + a.x;
            if (x_intersect > point.x + eps) {
                inside = !inside;
            }
        }
    }
    return inside;
}

std::vector<Point> shifted_polygon(const std::vector<Point>& polygon, int x, int y) {
    std::vector<Point> result;
    result.reserve(polygon.size());
    for (const Point& point : polygon) {
        result.push_back(shifted(point, x, y));
    }
    return result;
}

bool polygons_overlap_area(
    const std::vector<Point>& left,
    int left_x,
    int left_y,
    const std::vector<Point>& right,
    int right_x,
    int right_y
) {
    if (left.size() < 3 || right.size() < 3) {
        return false;
    }
    const std::vector<Point> a = shifted_polygon(left, left_x, left_y);
    const std::vector<Point> b = shifted_polygon(right, right_x, right_y);
    for (size_t i = 0; i < a.size(); ++i) {
        const Point& a0 = a[i];
        const Point& a1 = a[(i + 1) % a.size()];
        for (size_t j = 0; j < b.size(); ++j) {
            if (proper_segment_intersection(a0, a1, b[j], b[(j + 1) % b.size()])) {
                return true;
            }
        }
    }
    for (const Point& point : a) {
        if (strictly_inside_polygon(b, point)) {
            return true;
        }
    }
    for (const Point& point : b) {
        if (strictly_inside_polygon(a, point)) {
            return true;
        }
    }
    return false;
}

bool spatial_overlap(const Placement& a, const Placement& b, const Problem& problem) {
    if (g_overlap_mode == 2) {
        const auto& a_boxes = problem.blocks[a.block_id].layer_boxes[a.orient_idx];
        const auto& b_boxes = problem.blocks[b.block_id].layer_boxes[b.orient_idx];
        const auto& a_polys = problem.blocks[a.block_id].layer_polygons[a.orient_idx];
        const auto& b_polys = problem.blocks[b.block_id].layer_polygons[b.orient_idx];
        if (!a_polys.empty() && !b_polys.empty()) {
            const size_t shared_layers = std::min(a_polys.size(), b_polys.size());
            for (size_t layer_idx = 0; layer_idx < shared_layers; ++layer_idx) {
                if (layer_idx < a_boxes.size() && layer_idx < b_boxes.size()) {
                    const BBox& la = a_boxes[layer_idx];
                    const BBox& lb = b_boxes[layer_idx];
                    if (!overlap_rects(
                            a.x + la.min_x,
                            a.y + la.min_y,
                            a.x + la.max_x,
                            a.y + la.max_y,
                            b.x + lb.min_x,
                            b.y + lb.min_y,
                            b.x + lb.max_x,
                            b.y + lb.max_y
                        )) {
                        continue;
                    }
                }
                if (polygons_overlap_area(a_polys[layer_idx], a.x, a.y, b_polys[layer_idx], b.x, b.y)) {
                    return true;
                }
            }
            return false;
        }
    }
    if (g_overlap_mode == 1) {
        const auto& a_layers = problem.blocks[a.block_id].layer_boxes[a.orient_idx];
        const auto& b_layers = problem.blocks[b.block_id].layer_boxes[b.orient_idx];
        if (!a_layers.empty() && !b_layers.empty()) {
            const size_t shared_layers = std::min(a_layers.size(), b_layers.size());
            for (size_t layer_idx = 0; layer_idx < shared_layers; ++layer_idx) {
                const BBox& la = a_layers[layer_idx];
                const BBox& lb = b_layers[layer_idx];
                if (overlap_rects(
                        a.x + la.min_x,
                        a.y + la.min_y,
                        a.x + la.max_x,
                        a.y + la.max_y,
                        b.x + lb.min_x,
                        b.y + lb.min_y,
                        b.x + lb.max_x,
                        b.y + lb.max_y
                    )) {
                    return true;
                }
            }
            return false;
        }
    }
    return overlap_rects(
        rect_min_x(a, problem),
        rect_min_y(a, problem),
        rect_max_x(a, problem),
        rect_max_y(a, problem),
        rect_min_x(b, problem),
        rect_min_y(b, problem),
        rect_max_x(b, problem),
        rect_max_y(b, problem)
    );
}

bool has_polygon_layers(const Placement& placement, const Problem& problem) {
    const auto& polygons = problem.blocks[placement.block_id].layer_polygons[placement.orient_idx];
    return !polygons.empty();
}

bool same_height_collision(const Placement& a, const Placement& b, const Problem& problem) {
    if (!has_polygon_layers(a, problem) || !has_polygon_layers(b, problem)) {
        return spatial_overlap(a, b, problem);
    }
    const auto& a_boxes = problem.blocks[a.block_id].layer_boxes[a.orient_idx];
    const auto& b_boxes = problem.blocks[b.block_id].layer_boxes[b.orient_idx];
    const auto& a_polys = problem.blocks[a.block_id].layer_polygons[a.orient_idx];
    const auto& b_polys = problem.blocks[b.block_id].layer_polygons[b.orient_idx];
    const size_t shared_layers = std::min(a_polys.size(), b_polys.size());
    for (size_t layer_idx = 0; layer_idx < shared_layers; ++layer_idx) {
        if (layer_idx < a_boxes.size() && layer_idx < b_boxes.size()) {
            const BBox& la = a_boxes[layer_idx];
            const BBox& lb = b_boxes[layer_idx];
            if (!overlap_rects(
                    a.x + la.min_x,
                    a.y + la.min_y,
                    a.x + la.max_x,
                    a.y + la.max_y,
                    b.x + lb.min_x,
                    b.y + lb.min_y,
                    b.x + lb.max_x,
                    b.y + lb.max_y
                )) {
                continue;
            }
        }
        if (polygons_overlap_area(a_polys[layer_idx], a.x, a.y, b_polys[layer_idx], b.x, b.y)) {
            return true;
        }
    }
    return false;
}

bool crane_path_obstructed(const Placement& moving, const Placement& existing, const Problem& problem) {
    if (!has_polygon_layers(moving, problem) || !has_polygon_layers(existing, problem)) {
        return spatial_overlap(moving, existing, problem);
    }
    const auto& moving_boxes = problem.blocks[moving.block_id].layer_boxes[moving.orient_idx];
    const auto& existing_boxes = problem.blocks[existing.block_id].layer_boxes[existing.orient_idx];
    const auto& moving_polys = problem.blocks[moving.block_id].layer_polygons[moving.orient_idx];
    const auto& existing_polys = problem.blocks[existing.block_id].layer_polygons[existing.orient_idx];
    for (size_t k = 0; k < moving_polys.size(); ++k) {
        if (moving_polys[k].size() < 3) {
            continue;
        }
        for (size_t j = k; j < existing_polys.size(); ++j) {
            if (existing_polys[j].size() < 3) {
                continue;
            }
            if (k < moving_boxes.size() && j < existing_boxes.size()) {
                const BBox& mk = moving_boxes[k];
                const BBox& ej = existing_boxes[j];
            if (!overlap_rects(
                    moving.x + mk.min_x,
                    moving.y + mk.min_y,
                    moving.x + mk.max_x,
                    moving.y + mk.max_y,
                        existing.x + ej.min_x,
                    existing.y + ej.min_y,
                    existing.x + ej.max_x,
                    existing.y + ej.max_y
                )) {
                continue;
            }
        }
        if (polygons_overlap_area(moving_polys[k], moving.x, moving.y, existing_polys[j], existing.x, existing.y)) {
            return true;
        }
        }
    }
    return false;
}

bool entry_obstructed(
    const Problem& problem,
    const Placement& candidate,
    const std::vector<Placement>& placed,
    int entry_time
) {
    const BBox& box = problem.blocks[candidate.block_id].boxes[candidate.orient_idx];
    if (!fits(problem.bays[candidate.bay_id], box, candidate.x, candidate.y)) {
        return true;
    }
    if (!g_use_official_access) {
        return false;
    }
    for (const Placement& other : placed) {
        if (other.bay_id != candidate.bay_id || other.block_id == candidate.block_id) {
            continue;
        }
        if (!(other.entry < entry_time && entry_time < other.exit)) {
            continue;
        }
        if (crane_path_obstructed(candidate, other, problem)) {
            return true;
        }
    }
    return false;
}

bool exit_obstructed(
    const Problem& problem,
    const Placement& candidate,
    const std::vector<Placement>& placed,
    int exit_time
) {
    if (!g_use_official_access) {
        return false;
    }
    for (const Placement& other : placed) {
        if (other.bay_id != candidate.bay_id || other.block_id == candidate.block_id) {
            continue;
        }
        if (!(other.entry < exit_time && exit_time < other.exit)) {
            continue;
        }
        if (crane_path_obstructed(candidate, other, problem)) {
            return true;
        }
    }
    return false;
}

bool obstructs_existing_access(
    const Problem& problem,
    const Placement& candidate,
    const std::vector<Placement>& placed,
    int entry_time,
    int exit_time
) {
    if (!g_use_official_access) {
        return false;
    }
    for (const Placement& other : placed) {
        if (other.bay_id != candidate.bay_id || other.block_id == candidate.block_id) {
            continue;
        }
        if (entry_time < other.entry && other.entry < exit_time &&
            crane_path_obstructed(other, candidate, problem)) {
            return true;
        }
        if (entry_time < other.exit && other.exit < exit_time &&
            crane_path_obstructed(other, candidate, problem)) {
            return true;
        }
    }
    return false;
}

std::vector<std::pair<int, int>> candidate_positions(
    const Problem& problem,
    int bay_id,
    const BBox& box,
    const std::vector<Placement>& placed,
    int position_mode
) {
    const Bay& bay = problem.bays[bay_id];
    std::set<int> xs;
    std::set<int> ys;
    xs.insert(lower_x(box));
    ys.insert(lower_y(box));
    xs.insert(std::max(0, static_cast<int>(std::floor((bay.width - (box.max_x - box.min_x)) / 2.0 - box.min_x))));
    ys.insert(std::max(0, static_cast<int>(std::floor((bay.height - (box.max_y - box.min_y)) / 2.0 - box.min_y))));
    xs.insert(std::max(0, static_cast<int>(std::floor(bay.width - box.max_x))));
    ys.insert(std::max(0, static_cast<int>(std::floor(bay.height - box.max_y))));

    for (const Placement& other : placed) {
        if (other.bay_id != bay_id) {
            continue;
        }
        xs.insert(std::max(0, static_cast<int>(std::ceil(rect_max_x(other, problem) - box.min_x - 1e-9))));
        ys.insert(std::max(0, static_cast<int>(std::ceil(rect_max_y(other, problem) - box.min_y - 1e-9))));
        xs.insert(std::max(0, static_cast<int>(std::floor(rect_min_x(other, problem) - box.max_x + 1e-9))));
        ys.insert(std::max(0, static_cast<int>(std::floor(rect_min_y(other, problem) - box.max_y + 1e-9))));
    }

    std::vector<std::pair<int, int>> positions;
    positions.reserve(xs.size() * ys.size());
    for (int x : xs) {
        for (int y : ys) {
            if (fits(bay, box, x, y)) {
                positions.emplace_back(x, y);
            }
        }
    }
    std::sort(positions.begin(), positions.end(), [&](const auto& a, const auto& b) {
        if (position_mode == 3) {
            if (a.first != b.first) {
                return a.first < b.first;
            }
            return a.second < b.second;
        }
        if (position_mode == 1) {
            if (a.first != b.first) {
                return a.first < b.first;
            }
            return a.second < b.second;
        }
        if (position_mode == 2) {
            const double ac = std::abs((a.first + box.max_x) - bay.width * 0.5) +
                              std::abs((a.second + box.max_y) - bay.height * 0.5);
            const double bc = std::abs((b.first + box.max_x) - bay.width * 0.5) +
                              std::abs((b.second + box.max_y) - bay.height * 0.5);
            if (ac != bc) {
                return ac < bc;
            }
        }
        if (a.second != b.second) {
            return a.second < b.second;
        }
        return a.first < b.first;
    });
    const size_t max_positions = position_mode == 3 ? 1000U : 220U;
    if (positions.size() > max_positions) {
        positions.resize(max_positions);
    }
    return positions;
}

int earliest_entry(
    const Problem& problem,
    const Placement& candidate,
    const std::vector<Placement>& placed
) {
    const Block& block = problem.blocks[candidate.block_id];
    if (!g_use_official_access) {
        int start = block.release;
        std::vector<Placement> conflicts;
        for (const Placement& other : placed) {
            if (other.bay_id != candidate.bay_id) {
                continue;
            }
            if (spatial_overlap(candidate, other, problem)) {
                conflicts.push_back(other);
            }
        }
        std::sort(conflicts.begin(), conflicts.end(), [](const Placement& a, const Placement& b) {
            if (a.entry != b.entry) {
                return a.entry < b.entry;
            }
            return a.exit < b.exit;
        });

        bool changed = true;
        int guard = 0;
        while (changed && guard++ <= static_cast<int>(conflicts.size()) + 2) {
            changed = false;
            for (const Placement& other : conflicts) {
                const int finish = start + block.processing;
                if (finish <= other.entry || start >= other.exit) {
                    continue;
                }
                start = other.exit;
                changed = true;
                break;
            }
        }
        return start;
    }

    std::set<int> starts;
    starts.insert(block.release);
    for (const Placement& other : placed) {
        if (other.bay_id != candidate.bay_id) {
            continue;
        }
        if (same_height_collision(candidate, other, problem) || crane_path_obstructed(candidate, other, problem)) {
            starts.insert(std::max(block.release, other.exit));
        }
    }

    for (int seed_start : starts) {
        int start = seed_start;
        bool changed = true;
        int guard = 0;
        while (changed && guard++ <= static_cast<int>(placed.size()) + 2) {
            changed = false;
            const int finish = start + block.processing;
            if (entry_obstructed(problem, candidate, placed, start) ||
                exit_obstructed(problem, candidate, placed, finish) ||
                obstructs_existing_access(problem, candidate, placed, start, finish)) {
                int next_start = start + 1;
                for (const Placement& other : placed) {
                    if (other.bay_id != candidate.bay_id) {
                        continue;
                    }
                    if (other.entry <= start && start < other.exit) {
                        next_start = std::max(next_start, other.exit);
                    }
                    if (other.entry < finish && finish < other.exit) {
                        next_start = std::max(next_start, other.exit);
                    }
                    if (same_height_collision(candidate, other, problem) &&
                        start < other.exit && other.entry < finish) {
                        next_start = std::max(next_start, other.exit);
                    }
                    if (start < other.entry && other.entry < finish &&
                        crane_path_obstructed(other, candidate, problem)) {
                        next_start = std::max(next_start, other.exit);
                    }
                    if (start < other.exit && other.exit < finish &&
                        crane_path_obstructed(other, candidate, problem)) {
                        next_start = std::max(next_start, other.exit);
                    }
                }
                if (next_start <= start) {
                    next_start = start + 1;
                }
                start = next_start;
                changed = true;
            }
        }
        const int finish = start + block.processing;
        if (!entry_obstructed(problem, candidate, placed, start) &&
            !exit_obstructed(problem, candidate, placed, finish) &&
            !obstructs_existing_access(problem, candidate, placed, start, finish)) {
            return start;
        }
    }

    int fallback_start = block.release;
    for (const Placement& other : placed) {
        if (other.bay_id == candidate.bay_id) {
            fallback_start = std::max(fallback_start, other.exit);
        }
    }
    return fallback_start;
}

std::vector<double> bay_weights(const Problem& problem) {
    std::vector<double> areas;
    areas.reserve(problem.bays.size());
    double sum = 0.0;
    for (const Bay& bay : problem.bays) {
        const double area = std::max(1e-9, bay.width * bay.height);
        areas.push_back(area);
        sum += area;
    }
    const double average = sum / std::max<size_t>(1, areas.size());
    std::vector<double> weights;
    weights.reserve(areas.size());
    for (double area : areas) {
        weights.push_back(average / area);
    }
    return weights;
}

double insertion_score(
    const Problem& problem,
    const std::vector<Placement>& placed,
    const std::vector<double>& loads,
    const std::vector<double>& weights,
    const Placement& placement
) {
    const Block& block = problem.blocks[placement.block_id];
    const int s_max = *std::max_element(block.preferences.begin(), block.preferences.end());
    const double tardiness = std::max(0, placement.exit - block.due);
    const double pref_penalty = s_max - block.preferences[placement.bay_id];
    const double new_load = loads[placement.bay_id] + block.workload;
    double imbalance = 0.0;
    for (size_t other = 0; other < loads.size(); ++other) {
        if (static_cast<int>(other) == placement.bay_id) {
            continue;
        }
        imbalance = std::max(
            imbalance,
            std::abs(weights[placement.bay_id] * new_load - weights[other] * loads[other])
        );
    }
    double due_conflict = 0.0;
    for (const Placement& other : placed) {
        if (other.bay_id != placement.bay_id) {
            continue;
        }
        if (!spatial_overlap(placement, other, problem)) {
            continue;
        }
        const Block& other_block = problem.blocks[other.block_id];
        const int due_gap = std::abs(block.due - other_block.due);
        const int spacing_need = std::min(120, block.processing + other_block.processing);
        due_conflict += std::max(0, spacing_need - due_gap);
    }
    return problem.w1 * tardiness + problem.w2 * imbalance + problem.w3 * pref_penalty +
           problem.w1 * g_due_spacing_weight * due_conflict +
           1e-3 * placement.exit + 1e-4 * rect_max_y(placement, problem);
}

Candidate best_candidate_for_block(
    const Problem& problem,
    const std::vector<Placement>& placed,
    const std::vector<double>& loads,
    const std::vector<double>& weights,
    int block_id,
    const Timer& timer,
    int bay_mode,
    int position_mode
) {
    Candidate best;
    const Block& block = problem.blocks[block_id];
    std::vector<int> bay_order(problem.bays.size());
    for (size_t i = 0; i < bay_order.size(); ++i) {
        bay_order[i] = static_cast<int>(i);
    }
    std::sort(bay_order.begin(), bay_order.end(), [&](int a, int b) {
        const double area_a = problem.bays[a].width * problem.bays[a].height;
        const double area_b = problem.bays[b].width * problem.bays[b].height;
        if (bay_mode == 1) {
            if (block.preferences[a] != block.preferences[b]) {
                return block.preferences[a] > block.preferences[b];
            }
            if (loads[a] != loads[b]) {
                return loads[a] < loads[b];
            }
        } else if (bay_mode == 2) {
            if (area_a != area_b) {
                return area_a > area_b;
            }
            if (loads[a] != loads[b]) {
                return loads[a] < loads[b];
            }
        } else if (bay_mode == 3) {
            const double wa = weights[a] * loads[a];
            const double wb = weights[b] * loads[b];
            if (wa != wb) {
                return wa < wb;
            }
            if (block.preferences[a] != block.preferences[b]) {
                return block.preferences[a] > block.preferences[b];
            }
        } else {
            if (loads[a] != loads[b]) {
                return loads[a] < loads[b];
            }
            if (block.preferences[a] != block.preferences[b]) {
                return block.preferences[a] > block.preferences[b];
            }
        }
        return a < b;
    });

    for (int bay_id : bay_order) {
        if (timer.expired()) {
            break;
        }
        for (int orient_idx = 0; orient_idx < static_cast<int>(block.boxes.size()); ++orient_idx) {
            if (timer.expired()) {
                break;
            }
            const BBox& box = block.boxes[orient_idx];
            for (const auto& [x, y] : candidate_positions(problem, bay_id, box, placed, position_mode)) {
                if (timer.expired()) {
                    break;
                }
                Placement placement;
                placement.block_id = block_id;
                placement.bay_id = bay_id;
                placement.orient_idx = orient_idx;
                placement.x = x;
                placement.y = y;
                placement.entry = earliest_entry(problem, placement, placed);
                placement.exit = placement.entry + block.processing;
                placement.workload = block.workload;

                const double score = insertion_score(problem, placed, loads, weights, placement);
                if (!best.ok || score < best.score) {
                    best.ok = true;
                    best.score = score;
                    best.placement = placement;
                }
            }
        }
    }
    return best;
}

Candidate best_obj1_candidate_for_block(
    const Problem& problem,
    const std::vector<Placement>& placed,
    const std::vector<double>& loads,
    const std::vector<double>& weights,
    int block_id,
    const Timer& timer,
    int bay_mode,
    int position_mode
) {
    Candidate best;
    const Block& block = problem.blocks[block_id];
    std::vector<int> bay_order(problem.bays.size());
    for (size_t i = 0; i < bay_order.size(); ++i) {
        bay_order[i] = static_cast<int>(i);
    }
    std::sort(bay_order.begin(), bay_order.end(), [&](int a, int b) {
        if (bay_mode == 1 && block.preferences[a] != block.preferences[b]) {
            return block.preferences[a] > block.preferences[b];
        }
        if (bay_mode == 2) {
            const double area_a = problem.bays[a].width * problem.bays[a].height;
            const double area_b = problem.bays[b].width * problem.bays[b].height;
            if (area_a != area_b) {
                return area_a > area_b;
            }
        }
        const double wa = weights[a] * loads[a];
        const double wb = weights[b] * loads[b];
        if (wa != wb) {
            return wa < wb;
        }
        if (block.preferences[a] != block.preferences[b]) {
            return block.preferences[a] > block.preferences[b];
        }
        return a < b;
    });

    for (int bay_id : bay_order) {
        if (timer.expired()) {
            break;
        }
        for (int orient_idx = 0; orient_idx < static_cast<int>(block.boxes.size()); ++orient_idx) {
            if (timer.expired()) {
                break;
            }
            const BBox& box = block.boxes[orient_idx];
            for (const auto& [x, y] : candidate_positions(problem, bay_id, box, placed, position_mode)) {
                if (timer.expired()) {
                    break;
                }
                Placement placement;
                placement.block_id = block_id;
                placement.bay_id = bay_id;
                placement.orient_idx = orient_idx;
                placement.x = x;
                placement.y = y;
                placement.entry = earliest_entry(problem, placement, placed);
                placement.exit = placement.entry + block.processing;
                placement.workload = block.workload;

                const int s_max = *std::max_element(block.preferences.begin(), block.preferences.end());
                const double tardiness = std::max(0, placement.exit - block.due);
                const double pref_penalty = s_max - block.preferences[placement.bay_id];
                const double score = 1e9 * tardiness + 1e5 * placement.exit + 1e2 * pref_penalty +
                                     insertion_score(problem, placed, loads, weights, placement) * 1e-3;
                if (!best.ok || score < best.score) {
                    best.ok = true;
                    best.score = score;
                    best.placement = placement;
                }
            }
        }
    }
    return best;
}

Placement empty_bay_fallback(
    const Problem& problem,
    const std::vector<Placement>& placed,
    const std::vector<double>& loads,
    int block_id
) {
    const Block& block = problem.blocks[block_id];
    int best_bay = 0;
    for (size_t bay_id = 1; bay_id < problem.bays.size(); ++bay_id) {
        if (loads[bay_id] < loads[best_bay]) {
            best_bay = static_cast<int>(bay_id);
        }
    }
    int start = block.release;
    for (const Placement& other : placed) {
        if (other.bay_id == best_bay) {
            start = std::max(start, other.exit);
        }
    }

    int orient_idx = 0;
    int x = 0;
    int y = 0;
    for (int idx = 0; idx < static_cast<int>(block.boxes.size()); ++idx) {
        const BBox& box = block.boxes[idx];
        const int lx = lower_x(box);
        const int ly = lower_y(box);
        if (fits(problem.bays[best_bay], box, lx, ly)) {
            orient_idx = idx;
            x = lx;
            y = ly;
            break;
        }
    }

    Placement placement;
    placement.block_id = block_id;
    placement.bay_id = best_bay;
    placement.orient_idx = orient_idx;
    placement.x = x;
    placement.y = y;
    placement.entry = start;
    placement.exit = start + block.processing;
    placement.workload = block.workload;
    return placement;
}

double max_bbox_area(const Block& block) {
    double best = 0.0;
    for (const BBox& box : block.boxes) {
        best = std::max(best, std::max(0.0, box.max_x - box.min_x) * std::max(0.0, box.max_y - box.min_y));
    }
    return best;
}

double preference_gap(const Block& block) {
    if (block.preferences.empty()) {
        return 0.0;
    }
    std::vector<int> prefs = block.preferences;
    std::sort(prefs.begin(), prefs.end(), std::greater<int>());
    return prefs.front() - (prefs.size() > 1 ? prefs[1] : 0);
}

std::vector<int> build_order(const Problem& problem, int order_mode) {
    std::vector<int> order(problem.blocks.size());
    for (size_t i = 0; i < order.size(); ++i) {
        order[i] = static_cast<int>(i);
    }
    std::sort(order.begin(), order.end(), [&](int a, int b) {
        const Block& ba = problem.blocks[a];
        const Block& bb = problem.blocks[b];
        const int slack_a = ba.due - ba.release - ba.processing;
        const int slack_b = bb.due - bb.release - bb.processing;
        if (order_mode == 1) {
            if (ba.release != bb.release) {
                return ba.release < bb.release;
            }
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
        } else if (order_mode == 2) {
            if (slack_a != slack_b) {
                return slack_a < slack_b;
            }
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
        } else if (order_mode == 3) {
            if (ba.processing != bb.processing) {
                return ba.processing > bb.processing;
            }
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
        } else if (order_mode == 4) {
            const double area_a = max_bbox_area(ba);
            const double area_b = max_bbox_area(bb);
            if (area_a != area_b) {
                return area_a > area_b;
            }
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
        } else if (order_mode == 5) {
            const double gap_a = preference_gap(ba);
            const double gap_b = preference_gap(bb);
            if (gap_a != gap_b) {
                return gap_a > gap_b;
            }
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
        } else if (order_mode == 6) {
            const int latest_entry_a = ba.due - ba.processing;
            const int latest_entry_b = bb.due - bb.processing;
            if (latest_entry_a != latest_entry_b) {
                return latest_entry_a < latest_entry_b;
            }
            if (ba.release != bb.release) {
                return ba.release < bb.release;
            }
            const double area_a = max_bbox_area(ba);
            const double area_b = max_bbox_area(bb);
            if (area_a != area_b) {
                return area_a > area_b;
            }
        } else if (order_mode == 7) {
            const double area_a = max_bbox_area(ba);
            const double area_b = max_bbox_area(bb);
            if (slack_a != slack_b) {
                return slack_a < slack_b;
            }
            if (area_a != area_b) {
                return area_a > area_b;
            }
            if (ba.release != bb.release) {
                return ba.release < bb.release;
            }
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
        } else {
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
            if (ba.release != bb.release) {
                return ba.release < bb.release;
            }
        }
        return a < b;
    });
    return order;
}

std::vector<Placement> construct_solution(
    const Problem& problem,
    const Timer& timer,
    int order_mode,
    int bay_mode,
    int position_mode
) {
    const std::vector<int> order = build_order(problem, order_mode);
    std::vector<Placement> placed;
    placed.reserve(problem.blocks.size());
    std::vector<double> loads(problem.bays.size(), 0.0);
    const std::vector<double> weights = bay_weights(problem);

    for (int block_id : order) {
        Candidate candidate = best_candidate_for_block(
            problem,
            placed,
            loads,
            weights,
            block_id,
            timer,
            bay_mode,
            position_mode
        );
        Placement placement = candidate.ok ? candidate.placement : empty_bay_fallback(problem, placed, loads, block_id);
        placed.push_back(placement);
        loads[placement.bay_id] += problem.blocks[block_id].workload;
    }
    std::sort(placed.begin(), placed.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });
    return placed;
}

bool release_slot_collides(
    const Problem& problem,
    const Placement& candidate,
    const std::vector<Placement>& placed
) {
    for (const Placement& other : placed) {
        if (other.bay_id != candidate.bay_id) {
            continue;
        }
        if (candidate.entry < other.exit && other.entry < candidate.exit &&
            same_height_collision(candidate, other, problem)) {
            return true;
        }
    }
    return false;
}

bool blocks_earlier_due_exits_proxy(
    const Problem& problem,
    const Placement& candidate,
    const std::vector<Placement>& placed
) {
    const Block& block = problem.blocks[candidate.block_id];
    for (const Placement& target : placed) {
        if (target.bay_id != candidate.bay_id) {
            continue;
        }
        const Block& target_block = problem.blocks[target.block_id];
        if (target_block.due > block.due) {
            continue;
        }
        if (!(candidate.entry < target.exit && target.exit < candidate.exit)) {
            continue;
        }
        if (crane_path_obstructed(target, candidate, problem)) {
            return true;
        }
    }
    return false;
}

double exit_blocking_penalty_units(
    const Problem& problem,
    const Placement& candidate,
    const std::vector<Placement>& placed,
    int limit
) {
    const Block& block = problem.blocks[candidate.block_id];
    std::vector<std::tuple<int, int, int, int, int>> targets;
    for (const Placement& target : placed) {
        if (target.bay_id != candidate.bay_id || target.block_id == candidate.block_id) {
            continue;
        }
        const Block& target_block = problem.blocks[target.block_id];
        const int desired_exit = std::max(target.entry + target_block.processing, target_block.due);
        if (!(target.entry < desired_exit && desired_exit < target.exit)) {
            continue;
        }
        if (!(candidate.entry < desired_exit && desired_exit < candidate.exit)) {
            continue;
        }
        const int target_tardiness = std::max(0, target.exit - target_block.due);
        if (target_block.due > block.due && target_tardiness <= 0) {
            continue;
        }
        if (!crane_path_obstructed(target, candidate, problem)) {
            continue;
        }
        const int target_slack = target_block.due - target_block.release - target_block.processing;
        targets.emplace_back(target_block.due, -target_tardiness, target_slack, target.block_id, target_tardiness);
    }
    std::sort(targets.begin(), targets.end());
    double penalty = 0.0;
    int used = 0;
    for (const auto& item : targets) {
        if (used++ >= limit) {
            break;
        }
        const int target_slack = std::get<2>(item);
        const int target_tardiness = std::get<4>(item);
        const int delay_units = std::max(1, std::min(8, target_tardiness));
        const double urgency = 1.0 + 0.25 * std::max(0, 3 - target_slack);
        penalty += std::min(20.0, delay_units * urgency);
    }
    return penalty;
}

std::vector<std::pair<int, int>> cheap_release_positions(const Problem& problem, int bay_id, const BBox& box) {
    const Bay& bay = problem.bays[bay_id];
    const double width = box.max_x - box.min_x;
    const double height = box.max_y - box.min_y;
    std::vector<std::pair<int, int>> positions{
        {lower_x(box), lower_y(box)},
        {
            std::max(0, static_cast<int>(std::floor((bay.width - width) / 2.0 - box.min_x))),
            std::max(0, static_cast<int>(std::floor((bay.height - height) / 2.0 - box.min_y)))
        },
        {std::max(0, static_cast<int>(std::floor(bay.width - box.max_x))), lower_y(box)},
        {lower_x(box), std::max(0, static_cast<int>(std::floor(bay.height - box.max_y)))}
    };
    std::vector<std::pair<int, int>> unique;
    for (const auto& position : positions) {
        if (std::find(unique.begin(), unique.end(), position) == unique.end() &&
            fits(bay, box, position.first, position.second)) {
            unique.push_back(position);
        }
    }
    return unique;
}

Candidate fast_release_candidate_for_block(
    const Problem& problem,
    const std::vector<Placement>& placed,
    const std::vector<double>& loads,
    const std::vector<double>& weights,
    int block_id
) {
    Candidate best;
    const Block& block = problem.blocks[block_id];
    const int s_max = *std::max_element(block.preferences.begin(), block.preferences.end());
    std::vector<int> bay_order(problem.bays.size());
    for (size_t i = 0; i < bay_order.size(); ++i) {
        bay_order[i] = static_cast<int>(i);
    }
    std::sort(bay_order.begin(), bay_order.end(), [&](int a, int b) {
        if (loads[a] != loads[b]) {
            return loads[a] < loads[b];
        }
        if (block.preferences[a] != block.preferences[b]) {
            return block.preferences[a] > block.preferences[b];
        }
        return a < b;
    });

    for (int bay_id : bay_order) {
        for (int orient_idx = 0; orient_idx < static_cast<int>(block.boxes.size()); ++orient_idx) {
            const BBox& box = block.boxes[orient_idx];
            for (const auto& [x, y] : cheap_release_positions(problem, bay_id, box)) {
                Placement placement;
                placement.block_id = block_id;
                placement.bay_id = bay_id;
                placement.orient_idx = orient_idx;
                placement.x = x;
                placement.y = y;
                placement.entry = block.release;
                placement.exit = block.release + block.processing;
                placement.workload = block.workload;
                if (release_slot_collides(problem, placement, placed)) {
                    continue;
                }
                if (blocks_earlier_due_exits_proxy(problem, placement, placed)) {
                    continue;
                }
                const double blocker_penalty = exit_blocking_penalty_units(problem, placement, placed, 8);
                const double pref_penalty = s_max - block.preferences[bay_id];
                const double score = 1e9 * std::max(0, placement.exit - block.due) +
                                     1e6 * blocker_penalty +
                                     problem.w2 * weights[bay_id] * (loads[bay_id] + block.workload) +
                                     problem.w3 * pref_penalty +
                                     1e-3 * (y + box.max_y);
                if (blocker_penalty <= 1e-6) {
                    best.ok = true;
                    best.score = score;
                    best.placement = placement;
                    return best;
                }
                if (!best.ok || score < best.score) {
                    best.ok = true;
                    best.score = score;
                    best.placement = placement;
                }
            }
        }
    }
    return best;
}

std::vector<Placement> construct_release_batch_seed(const Problem& problem, const Timer& timer) {
    std::vector<int> order(problem.blocks.size());
    for (size_t i = 0; i < order.size(); ++i) {
        order[i] = static_cast<int>(i);
    }
    std::sort(order.begin(), order.end(), [&](int a, int b) {
        const Block& ba = problem.blocks[a];
        const Block& bb = problem.blocks[b];
        if (ba.release != bb.release) {
            return ba.release < bb.release;
        }
        if (ba.due != bb.due) {
            return ba.due < bb.due;
        }
        const double area_a = max_bbox_area(ba);
        const double area_b = max_bbox_area(bb);
        if (area_a != area_b) {
            return area_a > area_b;
        }
        return a < b;
    });

    std::vector<Placement> placed;
    placed.reserve(problem.blocks.size());
    std::vector<double> loads(problem.bays.size(), 0.0);
    const std::vector<double> weights = bay_weights(problem);
    for (int block_id : order) {
        Candidate candidate = fast_release_candidate_for_block(problem, placed, loads, weights, block_id);
        if (!candidate.ok) {
            candidate = best_obj1_candidate_for_block(problem, placed, loads, weights, block_id, timer, 0, 0);
        }
        Placement placement = candidate.ok ? candidate.placement : empty_bay_fallback(problem, placed, loads, block_id);
        placed.push_back(placement);
        loads[placement.bay_id] += problem.blocks[block_id].workload;
    }
    std::sort(placed.begin(), placed.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });
    return placed;
}

double solution_objective(const Problem& problem, const std::vector<Placement>& placements) {
    if (placements.size() != problem.blocks.size()) {
        return std::numeric_limits<double>::infinity();
    }
    std::vector<double> loads(problem.bays.size(), 0.0);
    double obj1 = 0.0;
    double obj3 = 0.0;
    for (const Placement& placement : placements) {
        const Block& block = problem.blocks[placement.block_id];
        if (placement.bay_id < 0 || placement.bay_id >= static_cast<int>(problem.bays.size())) {
            return std::numeric_limits<double>::infinity();
        }
        obj1 += std::max(0, placement.exit - block.due);
        loads[placement.bay_id] += block.workload;
        const int s_max = *std::max_element(block.preferences.begin(), block.preferences.end());
        obj3 += s_max - block.preferences[placement.bay_id];
    }
    const std::vector<double> weights = bay_weights(problem);
    double obj2 = 0.0;
    for (size_t i = 0; i < loads.size(); ++i) {
        for (size_t j = 0; j < loads.size(); ++j) {
            if (i == j) {
                continue;
            }
            obj2 = std::max(obj2, std::abs(weights[i] * loads[i] - weights[j] * loads[j]));
        }
    }
    return problem.w1 * obj1 + problem.w2 * std::floor(obj2) + problem.w3 * obj3;
}

double solution_obj1(const Problem& problem, const std::vector<Placement>& placements) {
    if (placements.size() != problem.blocks.size()) {
        return std::numeric_limits<double>::infinity();
    }
    double obj1 = 0.0;
    for (const Placement& placement : placements) {
        if (placement.block_id < 0 || placement.block_id >= static_cast<int>(problem.blocks.size())) {
            return std::numeric_limits<double>::infinity();
        }
        const Block& block = problem.blocks[placement.block_id];
        obj1 += std::max(0, placement.exit - block.due);
    }
    return obj1;
}

double solution_obj2_raw(const Problem& problem, const std::vector<Placement>& placements) {
    if (placements.size() != problem.blocks.size()) {
        return std::numeric_limits<double>::infinity();
    }
    std::vector<double> loads(problem.bays.size(), 0.0);
    for (const Placement& placement : placements) {
        if (placement.block_id < 0 || placement.block_id >= static_cast<int>(problem.blocks.size()) ||
            placement.bay_id < 0 || placement.bay_id >= static_cast<int>(problem.bays.size())) {
            return std::numeric_limits<double>::infinity();
        }
        loads[placement.bay_id] += problem.blocks[placement.block_id].workload;
    }
    const std::vector<double> weights = bay_weights(problem);
    double obj2 = 0.0;
    for (size_t i = 0; i < loads.size(); ++i) {
        for (size_t j = 0; j < loads.size(); ++j) {
            if (i != j) {
                obj2 = std::max(obj2, std::abs(weights[i] * loads[i] - weights[j] * loads[j]));
            }
        }
    }
    return std::floor(obj2);
}

bool obj1_rank_less(const Problem& problem, const std::vector<Placement>& a, const std::vector<Placement>& b) {
    const double a_obj1 = solution_obj1(problem, a);
    const double b_obj1 = solution_obj1(problem, b);
    if (std::abs(a_obj1 - b_obj1) > 1e-6) {
        return a_obj1 < b_obj1;
    }
    return solution_objective(problem, a) < solution_objective(problem, b);
}

bool solution_feasible_like(const Problem& problem, const std::vector<Placement>& placements) {
    if (placements.size() != problem.blocks.size()) {
        return false;
    }
    for (const Placement& placement : placements) {
        if (placement.block_id < 0 || placement.block_id >= static_cast<int>(problem.blocks.size())) {
            return false;
        }
        if (placement.bay_id < 0 || placement.bay_id >= static_cast<int>(problem.bays.size())) {
            return false;
        }
        const Block& block = problem.blocks[placement.block_id];
        if (placement.entry < block.release || placement.exit - placement.entry < block.processing) {
            return false;
        }
        if (!fits(problem.bays[placement.bay_id], block.boxes[placement.orient_idx], placement.x, placement.y)) {
            return false;
        }
    }

    for (const Placement& target : placements) {
        std::vector<Placement> present_at_entry;
        std::vector<Placement> present_at_exit;
        for (const Placement& other : placements) {
            if (other.bay_id != target.bay_id || other.block_id == target.block_id) {
                continue;
            }
            if (other.entry < target.entry && target.entry < other.exit) {
                present_at_entry.push_back(other);
            }
            if (other.entry < target.exit && target.exit < other.exit) {
                present_at_exit.push_back(other);
            }
        }
        if (entry_obstructed(problem, target, present_at_entry, target.entry)) {
            return false;
        }
        if (exit_obstructed(problem, target, present_at_exit, target.exit)) {
            return false;
        }
    }

    for (size_t i = 0; i < placements.size(); ++i) {
        for (size_t j = i + 1; j < placements.size(); ++j) {
            const Placement& a = placements[i];
            const Placement& b = placements[j];
            if (a.bay_id != b.bay_id) {
                continue;
            }
            if (!(a.entry < b.exit && b.entry < a.exit)) {
                continue;
            }
            if (same_height_collision(a, b, problem)) {
                return false;
            }
        }
    }
    struct Event {
        int time;
        int kind; // 0 = EXIT, 1 = ENTRY, matching cpp_accelerator.py operation order.
        int block_id;
    };
    std::vector<Event> events;
    events.reserve(placements.size() * 2);
    std::vector<Placement> by_block(problem.blocks.size());
    for (const Placement& placement : placements) {
        by_block[placement.block_id] = placement;
        events.push_back(Event{placement.exit, 0, placement.block_id});
        events.push_back(Event{placement.entry, 1, placement.block_id});
    }
    std::sort(events.begin(), events.end(), [](const Event& a, const Event& b) {
        if (a.time != b.time) {
            return a.time < b.time;
        }
        if (a.kind != b.kind) {
            return a.kind < b.kind;
        }
        return a.block_id < b.block_id;
    });
    std::vector<std::vector<char>> present(problem.bays.size(), std::vector<char>(problem.blocks.size(), 0));
    for (const Event& event : events) {
        const Placement& target = by_block[event.block_id];
        std::vector<Placement> present_placements;
        for (size_t block_id = 0; block_id < problem.blocks.size(); ++block_id) {
            if (present[target.bay_id][block_id]) {
                present_placements.push_back(by_block[block_id]);
            }
        }
        if (event.kind == 1) {
            if (entry_obstructed(problem, target, present_placements, target.entry)) {
                return false;
            }
            for (const Placement& other : present_placements) {
                if (other.bay_id == target.bay_id && other.entry == target.entry &&
                    crane_path_obstructed(target, other, problem)) {
                    return false;
                }
            }
            present[target.bay_id][target.block_id] = 1;
        } else {
            if (!present[target.bay_id][target.block_id]) {
                return false;
            }
            if (exit_obstructed(problem, target, present_placements, target.exit)) {
                return false;
            }
            present[target.bay_id][target.block_id] = 0;
        }
    }
    return true;
}

std::vector<double> loads_from_placements(const Problem& problem, const std::vector<Placement>& placements) {
    std::vector<double> loads(problem.bays.size(), 0.0);
    for (const Placement& placement : placements) {
        loads[placement.bay_id] += problem.blocks[placement.block_id].workload;
    }
    return loads;
}

std::vector<Placement> remove_block(const std::vector<Placement>& placements, int block_id) {
    std::vector<Placement> result;
    result.reserve(placements.size() - 1);
    for (const Placement& placement : placements) {
        if (placement.block_id != block_id) {
            result.push_back(placement);
        }
    }
    return result;
}

std::vector<int> relocation_order(const Problem& problem, const std::vector<Placement>& placements, int max_count) {
    std::vector<std::pair<std::tuple<int, int, int>, int>> ranked;
    ranked.reserve(placements.size());
    for (const Placement& placement : placements) {
        const Block& block = problem.blocks[placement.block_id];
        const int tardiness = std::max(0, placement.exit - block.due);
        const int slack = block.due - block.release - block.processing;
        ranked.push_back({std::make_tuple(-tardiness, slack, block.due), placement.block_id});
    }
    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        result.push_back(item.second);
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

std::vector<int> preference_relocation_order(const Problem& problem, const std::vector<Placement>& placements, int max_count) {
    std::vector<std::pair<std::tuple<int, int, int>, int>> ranked;
    ranked.reserve(placements.size());
    for (const Placement& placement : placements) {
        const Block& block = problem.blocks[placement.block_id];
        if (block.preferences.empty() || placement.bay_id < 0 ||
            placement.bay_id >= static_cast<int>(block.preferences.size())) {
            continue;
        }
        const int best_pref = *std::max_element(block.preferences.begin(), block.preferences.end());
        const int regret = best_pref - block.preferences[placement.bay_id];
        if (regret <= 0) {
            continue;
        }
        const int tardiness = std::max(0, placement.exit - block.due);
        ranked.push_back({std::make_tuple(-regret, tardiness, block.due), placement.block_id});
    }
    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        result.push_back(item.second);
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

bool blocks_access_at_exit_proxy(
    const Problem& problem,
    const Placement& target,
    const Placement& other
) {
    if (target.bay_id != other.bay_id || target.block_id == other.block_id) {
        return false;
    }
    if (g_use_official_access) {
        return crane_path_obstructed(target, other, problem);
    }
    return spatial_overlap(target, other, problem);
}

std::vector<int> exit_blockers_for_target(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int target_id,
    int max_count
) {
    Placement target;
    bool found = false;
    for (const Placement& placement : placements) {
        if (placement.block_id == target_id) {
            target = placement;
            found = true;
            break;
        }
    }
    if (!found) {
        return {};
    }
    const Block& block = problem.blocks[target_id];
    const int ideal_exit = std::max(block.due, block.release + block.processing);
    std::vector<std::pair<std::tuple<int, int, int>, int>> ranked;
    for (const Placement& other : placements) {
        if (other.block_id == target_id || other.bay_id != target.bay_id) {
            continue;
        }
        if (!(other.entry < ideal_exit && ideal_exit < other.exit)) {
            continue;
        }
        if (!blocks_access_at_exit_proxy(problem, target, other)) {
            continue;
        }
        const Block& other_block = problem.blocks[other.block_id];
        ranked.push_back(
            {
                std::make_tuple(
                    other_block.due < block.due ? 1 : 0,
                    other_block.due,
                    other.block_id
                ),
                other.block_id,
            }
        );
    }
    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        result.push_back(item.second);
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

std::vector<int> entry_blockers_for_target(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int target_id,
    int max_count
) {
    Placement target;
    bool found = false;
    for (const Placement& placement : placements) {
        if (placement.block_id == target_id) {
            target = placement;
            found = true;
            break;
        }
    }
    if (!found) {
        return {};
    }

    const Block& block = problem.blocks[target_id];
    const int latest_non_tardy_entry = block.due - block.processing;
    const int desired_entry = std::max(block.release, std::min(target.entry - 1, latest_non_tardy_entry));
    if (desired_entry >= target.entry) {
        return {};
    }
    const int desired_exit = desired_entry + block.processing;

    std::vector<std::pair<std::tuple<int, int, int, int>, int>> ranked;
    for (const Placement& other : placements) {
        if (other.block_id == target_id || other.bay_id != target.bay_id) {
            continue;
        }
        const int overlap = std::min(desired_exit, other.exit) - std::max(desired_entry, other.entry);
        if (overlap <= 0) {
            continue;
        }
        if (g_use_official_access) {
            if (!same_height_collision(target, other, problem) && !crane_path_obstructed(target, other, problem)) {
                continue;
            }
        } else if (!spatial_overlap(target, other, problem)) {
            continue;
        }
        const Block& other_block = problem.blocks[other.block_id];
        const int other_tardy = std::max(0, other.exit - other_block.due);
        ranked.push_back(
            {
                std::make_tuple(
                    other_block.due <= block.due ? 1 : 0,
                    -overlap,
                    other_tardy,
                    other.block_id
                ),
                other.block_id,
            }
        );
    }

    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        result.push_back(item.second);
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

std::vector<int> time_window_blockers_for_target(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int target_id,
    int max_count
) {
    Placement target;
    bool found = false;
    for (const Placement& placement : placements) {
        if (placement.block_id == target_id) {
            target = placement;
            found = true;
            break;
        }
    }
    if (!found) {
        return {};
    }
    const Block& block = problem.blocks[target_id];
    const int desired_entry = std::max(block.release, block.due - block.processing);
    if (desired_entry >= target.entry) {
        return {};
    }
    const int desired_exit = desired_entry + block.processing;

    std::vector<std::pair<std::tuple<int, int, int, int>, int>> ranked;
    for (const Placement& other : placements) {
        if (other.block_id == target_id || other.bay_id != target.bay_id) {
            continue;
        }
        const int overlap = std::min(desired_exit, other.exit) - std::max(desired_entry, other.entry);
        if (overlap <= 0) {
            continue;
        }
        const Block& other_block = problem.blocks[other.block_id];
        const int other_tardy = std::max(0, other.exit - other_block.due);
        ranked.push_back(
            {
                std::make_tuple(
                    other_block.due <= block.due ? 0 : 1,
                    -overlap,
                    other_tardy,
                    other.block_id
                ),
                other.block_id,
            }
        );
    }
    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        result.push_back(item.second);
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

std::vector<int> time_overlap_blocks_for_target(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int target_id,
    int max_count
) {
    Placement target;
    bool found = false;
    for (const Placement& placement : placements) {
        if (placement.block_id == target_id) {
            target = placement;
            found = true;
            break;
        }
    }
    if (!found) {
        return {};
    }
    const Block& block = problem.blocks[target_id];
    const int desired_entry = std::max(block.release, block.due - block.processing);
    const int desired_exit = desired_entry + block.processing;
    std::vector<std::pair<std::tuple<int, int, int, int>, int>> ranked;
    for (const Placement& other : placements) {
        if (other.block_id == target_id) {
            continue;
        }
        const Block& other_block = problem.blocks[other.block_id];
        const int desired_overlap =
            std::min(desired_exit, other.exit) - std::max(desired_entry, other.entry);
        const int current_overlap =
            std::min(target.exit, other.exit) - std::max(target.entry, other.entry);
        const int overlap = std::max(desired_overlap, current_overlap);
        if (overlap <= 0) {
            continue;
        }
        const int same_bay = other.bay_id == target.bay_id ? 0 : 1;
        const int other_tardy = std::max(0, other.exit - other_block.due);
        ranked.push_back(
            {
                std::make_tuple(-overlap, same_bay, other_tardy, other.block_id),
                other.block_id,
            }
        );
    }
    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        result.push_back(item.second);
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

std::vector<int> obj1_repair_targets(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int max_count
) {
    std::vector<std::pair<std::tuple<int, int, int>, int>> ranked;
    ranked.reserve(placements.size());
    for (const Placement& placement : placements) {
        const Block& block = problem.blocks[placement.block_id];
        const int tardiness = std::max(0, placement.exit - block.due);
        if (tardiness <= 0) {
            continue;
        }
        ranked.push_back({std::make_tuple(-tardiness, block.due, placement.block_id), placement.block_id});
    }
    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        result.push_back(item.second);
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

std::vector<int> large_tardy_entry_targets(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int max_count
) {
    std::vector<std::pair<std::tuple<int, int, int, int>, int>> ranked;
    ranked.reserve(placements.size());
    for (const Placement& placement : placements) {
        const Block& block = problem.blocks[placement.block_id];
        const int tardiness = std::max(0, placement.exit - block.due);
        const int entry_delay = std::max(0, placement.entry - block.release);
        if (tardiness <= 0 || entry_delay <= 0) {
            continue;
        }
        const BBox& box = block.boxes[placement.orient_idx];
        const int area = static_cast<int>(std::round(
            std::max(0.0, box.max_x - box.min_x) * std::max(0.0, box.max_y - box.min_y)
        ));
        ranked.push_back(
            {
                std::make_tuple(-tardiness, -area, block.due, placement.block_id),
                placement.block_id,
            }
        );
    }
    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        result.push_back(item.second);
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

std::vector<Placement> reschedule_fixed_positions(
    const Problem& problem,
    const std::vector<Placement>& placements,
    const std::vector<int>& order
) {
    if (placements.size() != problem.blocks.size()) {
        return placements;
    }

    std::vector<Placement> by_block = placements;
    std::sort(by_block.begin(), by_block.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });

    std::vector<Placement> scheduled;
    scheduled.reserve(placements.size());
    std::vector<int> scheduled_flag(problem.blocks.size(), 0);

    for (int block_id : order) {
        Placement current = by_block[block_id];
        const Block& block = problem.blocks[block_id];
        current.entry = block.release;

        bool changed = true;
        int guard = 0;
        while (changed && guard++ <= static_cast<int>(scheduled.size()) + 2) {
            changed = false;
            const int finish = current.entry + block.processing;
            if (entry_obstructed(problem, current, scheduled, current.entry) ||
                exit_obstructed(problem, current, scheduled, finish) ||
                obstructs_existing_access(problem, current, scheduled, current.entry, finish)) {
                int next_entry = current.entry + 1;
                for (const Placement& other : scheduled) {
                    if (other.bay_id != current.bay_id) {
                        continue;
                    }
                    if (other.entry <= current.entry && current.entry < other.exit) {
                        next_entry = std::max(next_entry, other.exit);
                    }
                    if (other.entry < finish && finish < other.exit) {
                        next_entry = std::max(next_entry, other.exit);
                    }
                    if (same_height_collision(current, other, problem) &&
                        current.entry < other.exit && other.entry < finish) {
                        next_entry = std::max(next_entry, other.exit);
                    }
                    if (current.entry < other.entry && other.entry < finish &&
                        crane_path_obstructed(other, current, problem)) {
                        next_entry = std::max(next_entry, other.exit);
                    }
                    if (current.entry < other.exit && other.exit < finish &&
                        crane_path_obstructed(other, current, problem)) {
                        next_entry = std::max(next_entry, other.exit);
                    }
                }
                if (next_entry <= current.entry) {
                    next_entry = current.entry + 1;
                }
                current.entry = next_entry;
                changed = true;
                continue;
            }
            for (const Placement& other : scheduled) {
                if (other.bay_id != current.bay_id || !same_height_collision(current, other, problem)) {
                    continue;
                }
                if (finish <= other.entry || current.entry >= other.exit) {
                    continue;
                }
                current.entry = other.exit;
                changed = true;
                break;
            }
        }
        current.exit = current.entry + block.processing;
        scheduled.push_back(current);
        scheduled_flag[block_id] = 1;
    }

    for (const Placement& placement : by_block) {
        if (!scheduled_flag[placement.block_id]) {
            scheduled.push_back(placement);
        }
    }
    std::sort(scheduled.begin(), scheduled.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });
    return scheduled;
}

std::vector<int> current_schedule_order(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int order_mode
) {
    std::vector<int> order;
    order.reserve(placements.size());
    std::vector<Placement> by_block = placements;
    std::sort(by_block.begin(), by_block.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });
    for (const Placement& placement : placements) {
        order.push_back(placement.block_id);
    }
    std::sort(order.begin(), order.end(), [&](int a, int b) {
        const Placement& pa = by_block[a];
        const Placement& pb = by_block[b];
        const Block& ba = problem.blocks[a];
        const Block& bb = problem.blocks[b];
        const int tardy_a = std::max(0, pa.exit - ba.due);
        const int tardy_b = std::max(0, pb.exit - bb.due);
        if (order_mode == 0) {
            if (pa.entry != pb.entry) {
                return pa.entry < pb.entry;
            }
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
        } else if (order_mode == 1) {
            if (pa.exit != pb.exit) {
                return pa.exit < pb.exit;
            }
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
        } else {
            if (tardy_a != tardy_b) {
                return tardy_a > tardy_b;
            }
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
        }
        return a < b;
    });
    return order;
}

void reschedule_pass(const Problem& problem, std::vector<Placement>& best, const Timer& timer) {
    double best_objective = solution_objective(problem, best);
    const bool prefer_feasible = g_filter_feasible_like || g_use_official_access;
    bool best_feasible = !prefer_feasible || solution_feasible_like(problem, best);
    for (int order_mode = 0; order_mode < 6 && !timer.expired(80); ++order_mode) {
        std::vector<Placement> candidate = reschedule_fixed_positions(problem, best, build_order(problem, order_mode));
        const double objective = solution_objective(problem, candidate);
        const bool feasible = !prefer_feasible || solution_feasible_like(problem, candidate);
        if ((feasible && !best_feasible) || (feasible == best_feasible && objective + 1e-6 < best_objective)) {
            best.swap(candidate);
            best_objective = objective;
            best_feasible = feasible;
        }
    }
    for (int order_mode = 0; order_mode < 3 && !timer.expired(80); ++order_mode) {
        std::vector<Placement> candidate = reschedule_fixed_positions(
            problem,
            best,
            current_schedule_order(problem, best, order_mode)
        );
        const double objective = solution_objective(problem, candidate);
        const bool feasible = !prefer_feasible || solution_feasible_like(problem, candidate);
        if ((feasible && !best_feasible) || (feasible == best_feasible && objective + 1e-6 < best_objective)) {
            best.swap(candidate);
            best_objective = objective;
            best_feasible = feasible;
        }
    }
}

void left_shift_obj1_pass(const Problem& problem, std::vector<Placement>& best, const Timer& timer);
void relaxed_left_shift_obj1_pass(const Problem& problem, std::vector<Placement>& best, const Timer& timer);
std::vector<int> fill_unique_blocks(std::vector<int> base, const std::vector<int>& extra, int max_count);
std::vector<int> release_due_sequence(const Problem& problem, std::vector<int> block_ids);
std::vector<int> bay_blocks_near_tardy(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int bay_id,
    int target_id,
    int max_count
);

void obj1_reschedule_pass(const Problem& problem, std::vector<Placement>& best, const Timer& timer) {
    if (best.size() != problem.blocks.size() || timer.expired(160)) {
        return;
    }
    const bool prefer_feasible = g_filter_feasible_like || g_use_official_access;
    double best_obj1 = solution_obj1(problem, best);
    double best_objective = solution_objective(problem, best);
    bool best_feasible = !prefer_feasible || solution_feasible_like(problem, best);
    auto consider = [&](std::vector<Placement> candidate) {
        const bool feasible = !prefer_feasible || solution_feasible_like(problem, candidate);
        if (prefer_feasible && best_feasible && !feasible) {
            return;
        }
        const double candidate_obj1 = solution_obj1(problem, candidate);
        const double candidate_objective = solution_objective(problem, candidate);
        if ((feasible && !best_feasible) ||
            (feasible == best_feasible &&
             (candidate_obj1 + 1e-6 < best_obj1 ||
              (std::abs(candidate_obj1 - best_obj1) < 1e-6 &&
               candidate_objective + 1e-6 < best_objective)))) {
            best.swap(candidate);
            best_obj1 = candidate_obj1;
            best_objective = candidate_objective;
            best_feasible = feasible;
        }
    };

    for (int pass = 0; pass < 3 && best_obj1 > 0.0 && !timer.expired(160); ++pass) {
        for (int order_mode = 0; order_mode < 6 && !timer.expired(160); ++order_mode) {
            consider(reschedule_fixed_positions(problem, best, build_order(problem, order_mode)));
        }
        for (int order_mode = 0; order_mode < 3 && !timer.expired(160); ++order_mode) {
            consider(reschedule_fixed_positions(
                problem,
                best,
                current_schedule_order(problem, best, order_mode)
            ));
        }
        std::vector<int> tardy_due_order = obj1_repair_targets(
            problem,
            best,
            static_cast<int>(best.size())
        );
        tardy_due_order = fill_unique_blocks(
            tardy_due_order,
            build_order(problem, 0),
            static_cast<int>(best.size())
        );
        std::stable_sort(tardy_due_order.begin(), tardy_due_order.end(), [&](int a, int b) {
            const Placement& pa = best[a];
            const Placement& pb = best[b];
            const Block& ba = problem.blocks[a];
            const Block& bb = problem.blocks[b];
            const int tardy_a = std::max(0, pa.exit - ba.due);
            const int tardy_b = std::max(0, pb.exit - bb.due);
            if ((tardy_a > 0) != (tardy_b > 0)) {
                return tardy_a > 0;
            }
            if (ba.due != bb.due) {
                return ba.due < bb.due;
            }
            const int slack_a = ba.due - ba.release - ba.processing;
            const int slack_b = bb.due - bb.release - bb.processing;
            if (slack_a != slack_b) {
                return slack_a < slack_b;
            }
            return a < b;
        });
        if (!tardy_due_order.empty()) {
            consider(reschedule_fixed_positions(problem, best, tardy_due_order));
        }
        left_shift_obj1_pass(problem, best, timer);
        relaxed_left_shift_obj1_pass(problem, best, timer);
        best_obj1 = solution_obj1(problem, best);
        best_objective = solution_objective(problem, best);
        best_feasible = !prefer_feasible || solution_feasible_like(problem, best);
    }
}

bool placement_time_feasible_against_all(
    const Problem& problem,
    const Placement& candidate,
    const std::vector<Placement>& placements
) {
    const Block& block = problem.blocks[candidate.block_id];
    if (candidate.entry < block.release || candidate.exit - candidate.entry < block.processing) {
        return false;
    }
    if (candidate.bay_id < 0 || candidate.bay_id >= static_cast<int>(problem.bays.size())) {
        return false;
    }
    if (!fits(problem.bays[candidate.bay_id], block.boxes[candidate.orient_idx], candidate.x, candidate.y)) {
        return false;
    }

    std::vector<Placement> same_bay_others;
    for (const Placement& other : placements) {
        if (other.block_id == candidate.block_id || other.bay_id != candidate.bay_id) {
            continue;
        }
        same_bay_others.push_back(other);
        if (candidate.entry < other.exit && other.entry < candidate.exit &&
            same_height_collision(candidate, other, problem)) {
            return false;
        }
    }
    if (entry_obstructed(problem, candidate, same_bay_others, candidate.entry) ||
        exit_obstructed(problem, candidate, same_bay_others, candidate.exit) ||
        obstructs_existing_access(problem, candidate, same_bay_others, candidate.entry, candidate.exit)) {
        return false;
    }
    return true;
}

bool relaxed_time_feasible_against_all(
    const Problem& problem,
    const Placement& candidate,
    const std::vector<Placement>& placements
) {
    const Block& block = problem.blocks[candidate.block_id];
    if (candidate.entry < block.release || candidate.exit - candidate.entry < block.processing) {
        return false;
    }
    if (candidate.bay_id < 0 || candidate.bay_id >= static_cast<int>(problem.bays.size())) {
        return false;
    }
    if (!fits(problem.bays[candidate.bay_id], block.boxes[candidate.orient_idx], candidate.x, candidate.y)) {
        return false;
    }
    for (const Placement& other : placements) {
        if (other.block_id == candidate.block_id || other.bay_id != candidate.bay_id) {
            continue;
        }
        if (candidate.entry < other.exit && other.entry < candidate.exit &&
            same_height_collision(candidate, other, problem)) {
            return false;
        }
    }
    return true;
}

bool access_time_feasible_against_all(
    const Problem& problem,
    const Placement& candidate,
    const std::vector<Placement>& placements
) {
    const Block& block = problem.blocks[candidate.block_id];
    if (candidate.entry < block.release || candidate.exit - candidate.entry < block.processing) {
        return false;
    }
    if (candidate.bay_id < 0 || candidate.bay_id >= static_cast<int>(problem.bays.size())) {
        return false;
    }
    if (!fits(problem.bays[candidate.bay_id], block.boxes[candidate.orient_idx], candidate.x, candidate.y)) {
        return false;
    }
    for (const Placement& other : placements) {
        if (other.block_id == candidate.block_id || other.bay_id != candidate.bay_id) {
            continue;
        }
        if (candidate.entry < other.exit && other.entry < candidate.exit &&
            same_height_collision(candidate, other, problem)) {
            return false;
        }
        if (other.entry < candidate.entry && candidate.entry < other.exit &&
            crane_path_obstructed(candidate, other, problem)) {
            return false;
        }
        if (other.entry < candidate.exit && candidate.exit < other.exit &&
            crane_path_obstructed(candidate, other, problem)) {
            return false;
        }
        if (candidate.entry < other.entry && other.entry < candidate.exit &&
            crane_path_obstructed(other, candidate, problem)) {
            return false;
        }
        if (candidate.entry < other.exit && other.exit < candidate.exit &&
            crane_path_obstructed(other, candidate, problem)) {
            return false;
        }
    }
    return true;
}

std::vector<int> placement_access_blockers(
    const Problem& problem,
    const Placement& target,
    const std::vector<Placement>& placements,
    int max_count
) {
    std::vector<std::pair<std::tuple<int, int, int, int>, int>> ranked;
    const Block& target_block = problem.blocks[target.block_id];
    for (const Placement& other : placements) {
        if (other.block_id == target.block_id || other.bay_id != target.bay_id) {
            continue;
        }
        bool blocks = false;
        if (target.entry < other.exit && other.entry < target.exit &&
            same_height_collision(target, other, problem)) {
            blocks = true;
        }
        if (other.entry < target.entry && target.entry < other.exit &&
            crane_path_obstructed(target, other, problem)) {
            blocks = true;
        }
        if (other.entry < target.exit && target.exit < other.exit &&
            crane_path_obstructed(target, other, problem)) {
            blocks = true;
        }
        if (target.entry < other.entry && other.entry < target.exit &&
            crane_path_obstructed(other, target, problem)) {
            blocks = true;
        }
        if (target.entry < other.exit && other.exit < target.exit &&
            crane_path_obstructed(other, target, problem)) {
            blocks = true;
        }
        if (!blocks) {
            continue;
        }
        const Block& other_block = problem.blocks[other.block_id];
        const int other_tardy = std::max(0, other.exit - other_block.due);
        ranked.push_back(
            {
                std::make_tuple(
                    other_block.due <= target_block.due ? 1 : 0,
                    other_tardy > 0 ? 1 : 0,
                    other_block.due,
                    other.block_id
                ),
                other.block_id,
            }
        );
    }
    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        result.push_back(item.second);
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

void left_shift_obj1_pass(const Problem& problem, std::vector<Placement>& best, const Timer& timer) {
    if (best.size() != problem.blocks.size() || timer.expired(120)) {
        return;
    }
    double best_obj1 = solution_obj1(problem, best);
    double best_objective = solution_objective(problem, best);
    std::vector<int> order = obj1_repair_targets(problem, best, static_cast<int>(best.size()));
    if (order.empty()) {
        order = build_order(problem, 0);
    }

    bool changed = true;
    int pass = 0;
    while (changed && pass++ < 4 && !timer.expired(120)) {
        changed = false;
        for (int block_id : order) {
            if (timer.expired(120)) {
                break;
            }
            if (block_id < 0 || block_id >= static_cast<int>(best.size()) || best[block_id].block_id != block_id) {
                continue;
            }
            Placement current = best[block_id];
            const Block& block = problem.blocks[block_id];
            const int earliest = block.release;
            const int latest_improving_entry = std::min(current.entry - 1, block.due - block.processing);
            if (latest_improving_entry < earliest) {
                continue;
            }
            Placement chosen = current;
            for (int start = earliest; start <= latest_improving_entry; ++start) {
                Placement candidate = current;
                candidate.entry = start;
                candidate.exit = start + block.processing;
                if (placement_time_feasible_against_all(problem, candidate, best)) {
                    chosen = candidate;
                    break;
                }
            }
            if (chosen.entry >= current.entry) {
                continue;
            }
            std::vector<Placement> candidate_solution = best;
            candidate_solution[block_id] = chosen;
            const double candidate_obj1 = solution_obj1(problem, candidate_solution);
            const double candidate_objective = solution_objective(problem, candidate_solution);
            if (candidate_obj1 + 1e-6 < best_obj1 ||
                (std::abs(candidate_obj1 - best_obj1) < 1e-6 &&
                 candidate_objective + 1e-6 < best_objective)) {
                best.swap(candidate_solution);
                best_obj1 = candidate_obj1;
                best_objective = candidate_objective;
                changed = true;
            }
        }
        order = obj1_repair_targets(problem, best, static_cast<int>(best.size()));
        if (order.empty()) {
            break;
        }
    }
}

void relaxed_left_shift_obj1_pass(const Problem& problem, std::vector<Placement>& best, const Timer& timer) {
    if (g_use_official_access || best.size() != problem.blocks.size() || timer.expired(120)) {
        return;
    }
    double best_obj1 = solution_obj1(problem, best);
    double best_objective = solution_objective(problem, best);
    std::vector<int> order = obj1_repair_targets(problem, best, static_cast<int>(best.size()));
    if (order.empty()) {
        return;
    }

    bool changed = true;
    int pass = 0;
    while (changed && pass++ < 4 && !timer.expired(120)) {
        changed = false;
        for (int block_id : order) {
            if (timer.expired(120)) {
                break;
            }
            if (block_id < 0 || block_id >= static_cast<int>(best.size()) || best[block_id].block_id != block_id) {
                continue;
            }
            Placement current = best[block_id];
            const Block& block = problem.blocks[block_id];
            const int latest_improving_entry = std::min(current.entry - 1, block.due - block.processing);
            if (latest_improving_entry < block.release) {
                continue;
            }
            Placement chosen = current;
            for (int start = block.release; start <= latest_improving_entry; ++start) {
                Placement candidate = current;
                candidate.entry = start;
                candidate.exit = start + block.processing;
                if (relaxed_time_feasible_against_all(problem, candidate, best)) {
                    chosen = candidate;
                    break;
                }
            }
            if (chosen.entry >= current.entry) {
                continue;
            }
            std::vector<Placement> candidate_solution = best;
            candidate_solution[block_id] = chosen;
            const double candidate_obj1 = solution_obj1(problem, candidate_solution);
            const double candidate_objective = solution_objective(problem, candidate_solution);
            if (candidate_obj1 + 1e-6 < best_obj1 ||
                (std::abs(candidate_obj1 - best_obj1) < 1e-6 &&
                 candidate_objective + 1e-6 < best_objective)) {
                best.swap(candidate_solution);
                best_obj1 = candidate_obj1;
                best_objective = candidate_objective;
                changed = true;
            }
        }
        order = obj1_repair_targets(problem, best, static_cast<int>(best.size()));
        if (order.empty()) {
            break;
        }
    }
}

void relocate_pass(
    const Problem& problem,
    std::vector<Placement>& best,
    const Timer& timer,
    int bay_mode,
    int position_mode
) {
    double best_objective = solution_objective(problem, best);
    const std::vector<double> weights = bay_weights(problem);
    const int max_blocks = std::min<int>(static_cast<int>(best.size()), 90);
    for (int block_id : relocation_order(problem, best, max_blocks)) {
        if (timer.expired()) {
            break;
        }
        std::vector<Placement> remaining = remove_block(best, block_id);
        std::vector<double> loads = loads_from_placements(problem, remaining);
        Candidate candidate = best_candidate_for_block(
            problem,
            remaining,
            loads,
            weights,
            block_id,
            timer,
            bay_mode,
            position_mode
        );
        if (!candidate.ok) {
            continue;
        }
        remaining.push_back(candidate.placement);
        std::sort(remaining.begin(), remaining.end(), [](const Placement& a, const Placement& b) {
            return a.block_id < b.block_id;
        });
        const double objective = solution_objective(problem, remaining);
        if (objective + 1e-6 < best_objective) {
            best.swap(remaining);
            best_objective = objective;
        }
    }
}

void preference_relocate_pass(
    const Problem& problem,
    std::vector<Placement>& best,
    const Timer& timer,
    int position_mode
) {
    double best_objective = solution_objective(problem, best);
    const std::vector<double> weights = bay_weights(problem);
    const int max_blocks = std::min<int>(static_cast<int>(best.size()), 90);
    for (int block_id : preference_relocation_order(problem, best, max_blocks)) {
        if (timer.expired(120)) {
            break;
        }
        std::vector<Placement> remaining = remove_block(best, block_id);
        std::vector<double> loads = loads_from_placements(problem, remaining);
        Candidate candidate = best_candidate_for_block(
            problem,
            remaining,
            loads,
            weights,
            block_id,
            timer,
            1,
            position_mode
        );
        if (!candidate.ok) {
            continue;
        }
        remaining.push_back(candidate.placement);
        std::sort(remaining.begin(), remaining.end(), [](const Placement& a, const Placement& b) {
            return a.block_id < b.block_id;
        });
        const double objective = solution_objective(problem, remaining);
        if (objective + 1e-6 < best_objective) {
            best.swap(remaining);
            best_objective = objective;
        }
    }
}

Placement find_placement(const std::vector<Placement>& placements, int block_id) {
    for (const Placement& placement : placements) {
        if (placement.block_id == block_id) {
            return placement;
        }
    }
    return Placement{};
}

std::vector<int> pair_partners(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int target_id,
    int max_count
) {
    const Placement target = find_placement(placements, target_id);
    std::vector<std::pair<std::tuple<int, int, int>, int>> ranked;
    for (const Placement& other : placements) {
        if (other.block_id == target_id || other.bay_id != target.bay_id) {
            continue;
        }
        if (!spatial_overlap(target, other, problem)) {
            continue;
        }
        const int temporal_overlap = std::min(target.exit, other.exit) - std::max(target.entry, other.entry);
        const int due_overlap = std::min(problem.blocks[target_id].due, other.exit) -
                                std::max(problem.blocks[target_id].release, other.entry);
        if (temporal_overlap <= 0 && due_overlap <= 0 && other.exit <= problem.blocks[target_id].due) {
            continue;
        }
        const Block& other_block = problem.blocks[other.block_id];
        ranked.push_back(
            {
                std::make_tuple(
                    -std::max({temporal_overlap, due_overlap, 0}),
                    other_block.due,
                    other.block_id
                ),
                other.block_id,
            }
        );
    }
    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        result.push_back(item.second);
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

bool insert_block_sequence(
    const Problem& problem,
    std::vector<Placement>& placements,
    const std::vector<int>& sequence,
    const Timer& timer,
    int bay_mode,
    int position_mode
) {
    const std::vector<double> weights = bay_weights(problem);
    for (int block_id : sequence) {
        if (timer.expired(80)) {
            return false;
        }
        std::vector<double> loads = loads_from_placements(problem, placements);
        Candidate candidate = best_candidate_for_block(
            problem,
            placements,
            loads,
            weights,
            block_id,
            timer,
            bay_mode,
            position_mode
        );
        if (!candidate.ok) {
            return false;
        }
        placements.push_back(candidate.placement);
    }
    std::sort(placements.begin(), placements.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });
    return true;
}

std::vector<Placement> remove_blocks(const std::vector<Placement>& placements, const std::vector<int>& block_ids) {
    std::set<int> removed(block_ids.begin(), block_ids.end());
    std::vector<Placement> result;
    result.reserve(placements.size());
    for (const Placement& placement : placements) {
        if (!removed.count(placement.block_id)) {
            result.push_back(placement);
        }
    }
    return result;
}

std::vector<int> fill_unique_blocks(
    std::vector<int> selected,
    const std::vector<int>& fallback,
    int max_count
) {
    std::set<int> seen(selected.begin(), selected.end());
    for (int block_id : fallback) {
        if (static_cast<int>(selected.size()) >= max_count) {
            break;
        }
        if (!seen.count(block_id)) {
            selected.push_back(block_id);
            seen.insert(block_id);
        }
    }
    return selected;
}

std::vector<int> random_removal_order(const std::vector<Placement>& placements, Rng& rng, int max_count) {
    std::vector<int> ids;
    ids.reserve(placements.size());
    for (const Placement& placement : placements) {
        ids.push_back(placement.block_id);
    }
    for (int i = 0; i < static_cast<int>(ids.size()); ++i) {
        const int j = i + rng.next_int(static_cast<int>(ids.size()) - i);
        std::swap(ids[i], ids[j]);
    }
    if (static_cast<int>(ids.size()) > max_count) {
        ids.resize(max_count);
    }
    return ids;
}

std::vector<int> alns_removal_set(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int iteration,
    Rng& rng
) {
    const int n = static_cast<int>(placements.size());
    const int remove_count = std::min(n, n >= 150 ? 5 + (iteration % 12) : 2 + (iteration % 4));
    std::vector<int> selected;
    if (remove_count <= 0) {
        return selected;
    }

    const std::vector<int> tardy_order = relocation_order(problem, placements, std::min(n, 120));
    if (n >= 150 && iteration % 6 >= 4) {
        std::vector<int> targets = large_tardy_entry_targets(problem, placements, 8);
        targets = fill_unique_blocks(targets, tardy_order, 12);
        if (!targets.empty()) {
            const int target_id = targets[iteration % static_cast<int>(targets.size())];
            const Placement current = find_placement(placements, target_id);
            const Block& block = problem.blocks[target_id];
            const int ideal_entry = std::max(block.release, block.due - block.processing);
            if (ideal_entry < current.entry) {
                Placement ideal_target = current;
                ideal_target.entry = ideal_entry;
                ideal_target.exit = ideal_entry + block.processing;
                std::vector<Placement> without_target = remove_block(placements, target_id);
                selected.push_back(target_id);
                selected = fill_unique_blocks(
                    selected,
                    placement_access_blockers(problem, ideal_target, without_target, remove_count),
                    remove_count
                );
                selected = fill_unique_blocks(
                    selected,
                    time_window_blockers_for_target(problem, placements, target_id, remove_count),
                    remove_count
                );
                selected = fill_unique_blocks(
                    selected,
                    entry_blockers_for_target(problem, placements, target_id, remove_count),
                    remove_count
                );
                selected = fill_unique_blocks(
                    selected,
                    pair_partners(problem, placements, target_id, remove_count),
                    remove_count
                );
                if (static_cast<int>(selected.size()) >= 2) {
                    return selected;
                }
            }
            selected.clear();
        }
    }
    if (iteration % 4 == 0) {
        selected = fill_unique_blocks(selected, tardy_order, remove_count);
    } else if (iteration % 4 == 1) {
        selected = fill_unique_blocks(
            selected,
            preference_relocation_order(problem, placements, std::min(n, 120)),
            remove_count
        );
        selected = fill_unique_blocks(selected, tardy_order, remove_count);
    } else if (iteration % 4 == 2) {
        if (!tardy_order.empty()) {
            selected.push_back(tardy_order.front());
            selected = fill_unique_blocks(
                selected,
                pair_partners(problem, placements, tardy_order.front(), remove_count - 1),
                remove_count
            );
        }
        selected = fill_unique_blocks(selected, tardy_order, remove_count);
    } else {
        selected = fill_unique_blocks(selected, random_removal_order(placements, rng, remove_count), remove_count);
    }
    return selected;
}

std::vector<int> due_order_sequence(const Problem& problem, std::vector<int> block_ids) {
    std::sort(block_ids.begin(), block_ids.end(), [&](int a, int b) {
        const Block& ba = problem.blocks[a];
        const Block& bb = problem.blocks[b];
        if (ba.due != bb.due) {
            return ba.due < bb.due;
        }
        if (ba.release != bb.release) {
            return ba.release < bb.release;
        }
        return a < b;
    });
    return block_ids;
}

std::vector<int> current_entry_sequence(
    const Problem& problem,
    const std::vector<Placement>& placements,
    std::vector<int> block_ids
) {
    std::sort(block_ids.begin(), block_ids.end(), [&](int a, int b) {
        const Placement pa = find_placement(placements, a);
        const Placement pb = find_placement(placements, b);
        if (pa.entry != pb.entry) {
            return pa.entry < pb.entry;
        }
        const Block& ba = problem.blocks[a];
        const Block& bb = problem.blocks[b];
        if (ba.due != bb.due) {
            return ba.due < bb.due;
        }
        return a < b;
    });
    return block_ids;
}

std::vector<int> tardy_first_sequence(
    const Problem& problem,
    const std::vector<Placement>& placements,
    std::vector<int> block_ids
) {
    std::sort(block_ids.begin(), block_ids.end(), [&](int a, int b) {
        const Placement pa = find_placement(placements, a);
        const Placement pb = find_placement(placements, b);
        const int tardy_a = std::max(0, pa.exit - problem.blocks[a].due);
        const int tardy_b = std::max(0, pb.exit - problem.blocks[b].due);
        if (tardy_a != tardy_b) {
            return tardy_a > tardy_b;
        }
        if (problem.blocks[a].due != problem.blocks[b].due) {
            return problem.blocks[a].due < problem.blocks[b].due;
        }
        return a < b;
    });
    return block_ids;
}

bool repair_sequence(
    const Problem& problem,
    std::vector<Placement>& placements,
    const std::vector<int>& sequence,
    const Timer& timer,
    int bay_mode,
    int position_mode
) {
    const std::vector<double> weights = bay_weights(problem);
    for (int block_id : sequence) {
        if (timer.expired(80)) {
            return false;
        }
        std::vector<double> loads = loads_from_placements(problem, placements);
        Candidate candidate = best_candidate_for_block(
            problem,
            placements,
            loads,
            weights,
            block_id,
            timer,
            bay_mode,
            position_mode
        );
        if (!candidate.ok) {
            return false;
        }
        placements.push_back(candidate.placement);
    }
    std::sort(placements.begin(), placements.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });
    return placements.size() == problem.blocks.size();
}

bool repair_sequence_obj1(
    const Problem& problem,
    std::vector<Placement>& placements,
    const std::vector<int>& sequence,
    const Timer& timer,
    int bay_mode,
    int position_mode
) {
    const std::vector<double> weights = bay_weights(problem);
    for (int block_id : sequence) {
        if (timer.expired(80)) {
            return false;
        }
        std::vector<double> loads = loads_from_placements(problem, placements);
        Candidate candidate = best_obj1_candidate_for_block(
            problem,
            placements,
            loads,
            weights,
            block_id,
            timer,
            bay_mode,
            position_mode
        );
        if (!candidate.ok) {
            return false;
        }
        placements.push_back(candidate.placement);
    }
    std::sort(placements.begin(), placements.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });
    return placements.size() == problem.blocks.size();
}

std::vector<int> critical_due_cluster(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int count
) {
    if (count <= 0) {
        return {};
    }
    std::vector<int> targets = obj1_repair_targets(problem, placements, 1);
    if (targets.empty()) {
        return {};
    }
    const int target_id = targets.front();
    const Placement target = find_placement(placements, target_id);
    const Block& target_block = problem.blocks[target_id];
    std::vector<int> selected{target_id};
    std::vector<std::pair<std::tuple<int, int, int, int, int, int>, int>> ranked;
    for (const Placement& other : placements) {
        if (other.block_id == target_id || other.bay_id != target.bay_id) {
            continue;
        }
        const int current_overlap = std::min(target.exit, other.exit) - std::max(target.entry, other.entry);
        const int due_window_overlap =
            std::min(target_block.due, other.exit) - std::max(target_block.release, other.entry);
        const bool spans_due = other.entry < target_block.due && target_block.due < other.exit;
        const bool spans_release = other.entry < target_block.release && target_block.release < other.exit;
        if (current_overlap <= 0 && due_window_overlap <= 0 && !spans_due && !spans_release) {
            continue;
        }
        const Block& other_block = problem.blocks[other.block_id];
        ranked.push_back(
            {
                std::make_tuple(
                    other_block.due < target_block.due ? 1 : 0,
                    spans_due ? 0 : 1,
                    spans_release ? 0 : 1,
                    -std::max({current_overlap, due_window_overlap, 0}),
                    std::abs(other_block.due - target_block.due),
                    other.exit
                ),
                other.block_id,
            }
        );
    }
    std::sort(ranked.begin(), ranked.end());
    for (const auto& item : ranked) {
        selected.push_back(item.second);
        if (static_cast<int>(selected.size()) >= count) {
            break;
        }
    }
    return selected;
}

std::vector<int> access_blocker_cluster(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int count
) {
    if (count <= 0) {
        return {};
    }
    std::vector<int> selected;
    std::set<int> seen;
    std::vector<int> targets = obj1_repair_targets(problem, placements, std::max(1, std::min(4, count)));
    for (int target_id : targets) {
        if (static_cast<int>(selected.size()) >= count) {
            break;
        }
        const Placement target = find_placement(placements, target_id);
        if (!seen.count(target_id)) {
            selected.push_back(target_id);
            seen.insert(target_id);
        }
        if (static_cast<int>(selected.size()) >= count) {
            break;
        }
        std::vector<std::pair<std::tuple<int, int, int, int, int>, int>> ranked;
        for (const Placement& other : placements) {
            if (other.block_id == target_id || seen.count(other.block_id) || other.bay_id != target.bay_id) {
                continue;
            }
            const int overlap = std::min(target.exit, other.exit) - std::max(target.entry, other.entry);
            const bool spans_entry = other.entry < target.entry && target.entry < other.exit;
            const bool spans_exit = other.entry < target.exit && target.exit < other.exit;
            if (overlap <= 0 && !spans_entry && !spans_exit) {
                continue;
            }
            ranked.push_back(
                {
                    std::make_tuple(
                        spans_entry ? 0 : 1,
                        spans_exit ? 0 : 1,
                        -std::max(0, overlap),
                        problem.blocks[other.block_id].due,
                        other.block_id
                    ),
                    other.block_id,
                }
            );
        }
        std::sort(ranked.begin(), ranked.end());
        for (const auto& item : ranked) {
            if (seen.count(item.second)) {
                continue;
            }
            selected.push_back(item.second);
            seen.insert(item.second);
            if (static_cast<int>(selected.size()) >= count) {
                break;
            }
        }
    }
    if (static_cast<int>(selected.size()) < count) {
        for (int block_id : obj1_repair_targets(problem, placements, count)) {
            if (seen.count(block_id)) {
                continue;
            }
            selected.push_back(block_id);
            seen.insert(block_id);
            if (static_cast<int>(selected.size()) >= count) {
                break;
            }
        }
    }
    return selected;
}

std::vector<int> actual_exit_blocker_cluster(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int count
) {
    if (count <= 0) {
        return {};
    }
    std::vector<int> targets = obj1_repair_targets(problem, placements, 1);
    if (targets.empty()) {
        return {};
    }
    std::vector<int> selected{targets.front()};
    selected = fill_unique_blocks(selected, exit_blockers_for_target(problem, placements, selected.front(), count - 1), count);
    if (static_cast<int>(selected.size()) < count) {
        selected = fill_unique_blocks(selected, pair_partners(problem, placements, selected.front(), count), count);
    }
    return selected;
}

bool python_style_obj1_polish_once(
    const Problem& problem,
    std::vector<Placement>& incumbent,
    const Timer& timer,
    int iteration
) {
    const double incumbent_obj1 = solution_obj1(problem, incumbent);
    if (incumbent_obj1 <= 0.0 || timer.expired(180)) {
        return false;
    }
    const double incumbent_objective = solution_objective(problem, incumbent);
    std::vector<Placement> best_candidate = incumbent;
    double best_obj1 = incumbent_obj1;
    double best_objective = incumbent_objective;
    bool improved = false;

    auto add_cluster = [](std::vector<std::vector<int>>& clusters, std::vector<int> cluster) {
        if (cluster.empty()) {
            return;
        }
        std::vector<int> key = cluster;
        std::sort(key.begin(), key.end());
        key.erase(std::unique(key.begin(), key.end()), key.end());
        for (const auto& existing : clusters) {
            std::vector<int> existing_key = existing;
            std::sort(existing_key.begin(), existing_key.end());
            existing_key.erase(std::unique(existing_key.begin(), existing_key.end()), existing_key.end());
            if (existing_key == key) {
                return;
            }
        }
        clusters.push_back(std::move(cluster));
    };

    std::vector<std::vector<int>> clusters;
    const int max_count = problem.blocks.size() < 150 ? 2 : 4;
    for (int count = 1; count <= max_count; ++count) {
        add_cluster(clusters, obj1_repair_targets(problem, incumbent, count));
        add_cluster(clusters, critical_due_cluster(problem, incumbent, count));
        if (count >= 2) {
            add_cluster(clusters, access_blocker_cluster(problem, incumbent, count));
            add_cluster(clusters, actual_exit_blocker_cluster(problem, incumbent, count));
        }
    }

    auto consider_candidate = [&](std::vector<Placement> candidate) {
        if (candidate.size() != problem.blocks.size()) {
            return;
        }
        std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
            return a.block_id < b.block_id;
        });
        const double candidate_obj1 = solution_obj1(problem, candidate);
        const double candidate_objective = solution_objective(problem, candidate);
        if (candidate_obj1 + 1e-6 < best_obj1 ||
            (std::abs(candidate_obj1 - best_obj1) < 1e-6 &&
             candidate_objective + 1e-6 < best_objective)) {
            best_candidate.swap(candidate);
            best_obj1 = candidate_obj1;
            best_objective = candidate_objective;
            improved = true;
        }
    };

    const int bay_start = iteration % 4;
    const int pos_start = (iteration / 4) % 3;
    for (const std::vector<int>& cluster : clusters) {
        if (timer.expired(180)) {
            break;
        }
        std::vector<std::vector<int>> sequences;
        sequences.push_back(due_order_sequence(problem, cluster));
        sequences.push_back(cluster);
        sequences.push_back(tardy_first_sequence(problem, incumbent, cluster));
        sequences.push_back(current_entry_sequence(problem, incumbent, cluster));
        for (const std::vector<int>& sequence : sequences) {
            if (timer.expired(180)) {
                break;
            }
            for (int bay_offset = 0; bay_offset < 4 && !timer.expired(180); ++bay_offset) {
                for (int pos_offset = 0; pos_offset < 3 && !timer.expired(180); ++pos_offset) {
                    const int bay_mode = (bay_start + bay_offset) % 4;
                    const int position_mode = (pos_start + pos_offset) % 3;
                    for (int repair_mode = 0; repair_mode < 2 && !timer.expired(180); ++repair_mode) {
                        std::vector<Placement> candidate = remove_blocks(incumbent, sequence);
                        const bool repaired = repair_mode == 0
                            ? repair_sequence_obj1(problem, candidate, sequence, timer, bay_mode, position_mode)
                            : repair_sequence(problem, candidate, sequence, timer, bay_mode, position_mode);
                        if (!repaired) {
                            continue;
                        }
                        consider_candidate(candidate);
                        left_shift_obj1_pass(problem, candidate, timer);
                        relaxed_left_shift_obj1_pass(problem, candidate, timer);
                        obj1_reschedule_pass(problem, candidate, timer);
                        consider_candidate(std::move(candidate));
                    }
                }
            }
        }
    }

    if (improved) {
        incumbent.swap(best_candidate);
    }
    return improved;
}

bool protect_tardy_entry_slot_once(
    const Problem& problem,
    std::vector<Placement>& incumbent,
    const Timer& timer,
    int iteration
) {
    const double incumbent_obj1 = solution_obj1(problem, incumbent);
    if (incumbent_obj1 <= 0.0 || timer.expired(220)) {
        return false;
    }
    double best_obj1 = incumbent_obj1;
    double best_objective = solution_objective(problem, incumbent);
    std::vector<Placement> best_candidate = incumbent;
    bool improved = false;

    std::vector<int> targets = obj1_repair_targets(problem, incumbent, 36);
    targets = fill_unique_blocks(targets, large_tardy_entry_targets(problem, incumbent, 36), 54);
    const int bay_start = iteration % 4;
    const int pos_start = (iteration / 4) % 3;

    auto consider = [&](std::vector<Placement> candidate) {
        if (candidate.size() != problem.blocks.size()) {
            return;
        }
        std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
            return a.block_id < b.block_id;
        });
        if (g_filter_feasible_like && !solution_feasible_like(problem, candidate)) {
            return;
        }
        const double obj1 = solution_obj1(problem, candidate);
        const double objective = solution_objective(problem, candidate);
        if (obj1 + 1e-6 < best_obj1 ||
            (std::abs(obj1 - best_obj1) < 1e-6 && objective + 1e-6 < best_objective)) {
            best_candidate.swap(candidate);
            best_obj1 = obj1;
            best_objective = objective;
            improved = true;
        }
    };

    for (int target_id : targets) {
        if (timer.expired(220)) {
            break;
        }
        const Placement current = find_placement(incumbent, target_id);
        const Block& block = problem.blocks[target_id];
        const int ideal_entry = std::max(block.release, block.due - block.processing);
        if (ideal_entry >= current.entry) {
            continue;
        }
        Placement protected_target = current;
        protected_target.entry = ideal_entry;
        protected_target.exit = ideal_entry + block.processing;

        std::vector<Placement> without_target = remove_blocks(incumbent, std::vector<int>{target_id});
        std::vector<int> blockers = placement_access_blockers(problem, protected_target, without_target, 18);
        blockers = fill_unique_blocks(blockers, time_window_blockers_for_target(problem, incumbent, target_id, 12), 24);
        blockers = fill_unique_blocks(blockers, entry_blockers_for_target(problem, incumbent, target_id, 12), 30);
        if (blockers.empty()) {
            blockers = pair_partners(problem, incumbent, target_id, 8);
        }
        if (blockers.empty()) {
            continue;
        }

        const int max_prefix = std::min<int>(static_cast<int>(blockers.size()), 10);
        for (int prefix = 1; prefix <= max_prefix && !timer.expired(220); ++prefix) {
            std::vector<int> removed{target_id};
            removed.insert(removed.end(), blockers.begin(), blockers.begin() + prefix);
            std::sort(removed.begin(), removed.end());
            removed.erase(std::unique(removed.begin(), removed.end()), removed.end());

            std::vector<Placement> base = remove_blocks(incumbent, removed);
            if (!placement_time_feasible_against_all(problem, protected_target, base)) {
                continue;
            }
            base.push_back(protected_target);

            std::vector<int> blocker_sequence;
            blocker_sequence.reserve(removed.size());
            for (int block_id : removed) {
                if (block_id != target_id) {
                    blocker_sequence.push_back(block_id);
                }
            }

            std::vector<std::vector<int>> sequences;
            sequences.push_back(due_order_sequence(problem, blocker_sequence));
            sequences.push_back(current_entry_sequence(problem, incumbent, blocker_sequence));
            sequences.push_back(tardy_first_sequence(problem, incumbent, blocker_sequence));

            for (const std::vector<int>& sequence : sequences) {
                if (timer.expired(220)) {
                    break;
                }
                for (int bay_offset = 0; bay_offset < 4 && !timer.expired(220); ++bay_offset) {
                    for (int pos_offset = 0; pos_offset < 3 && !timer.expired(220); ++pos_offset) {
                        const int bay_mode = (bay_start + bay_offset) % 4;
                        const int position_mode = (pos_start + pos_offset) % 3;
                        std::vector<Placement> candidate = base;
                        if (!repair_sequence_obj1(problem, candidate, sequence, timer, bay_mode, position_mode)) {
                            continue;
                        }
                        left_shift_obj1_pass(problem, candidate, timer);
                        relaxed_left_shift_obj1_pass(problem, candidate, timer);
                        obj1_reschedule_pass(problem, candidate, timer);
                        consider(std::move(candidate));
                    }
                }
            }
        }
    }

    if (improved) {
        incumbent.swap(best_candidate);
    }
    return improved;
}

bool entry_slot_chain_repair_iteration(
    const Problem& problem,
    std::vector<Placement>& incumbent,
    const Timer& timer,
    int iteration
) {
    const double incumbent_obj1 = solution_obj1(problem, incumbent);
    if (incumbent_obj1 <= 0.0 || timer.expired(180)) {
        return false;
    }
    double best_obj1 = incumbent_obj1;
    double best_objective = solution_objective(problem, incumbent);
    std::vector<Placement> best_candidate = incumbent;
    bool improved = false;

    std::vector<int> targets = large_tardy_entry_targets(problem, incumbent, 12);
    targets = fill_unique_blocks(targets, obj1_repair_targets(problem, incumbent, 12), 16);
    if (targets.empty()) {
        return false;
    }

    const int target_id = targets[iteration % static_cast<int>(targets.size())];
    const Placement current = find_placement(incumbent, target_id);
    const Block& block = problem.blocks[target_id];
    const int ideal_entry = std::max(block.release, block.due - block.processing);
    if (ideal_entry >= current.entry) {
        return false;
    }

    std::vector<Placement> without_target = remove_block(incumbent, target_id);
    const int bay_start = iteration % 4;
    const int pos_start = (iteration / 4) % 3;
    auto consider = [&](std::vector<Placement> candidate) {
        if (candidate.size() != problem.blocks.size()) {
            return;
        }
        std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
            return a.block_id < b.block_id;
        });
        const double obj1 = solution_obj1(problem, candidate);
        const double objective = solution_objective(problem, candidate);
        if (obj1 + 1e-6 < best_obj1 ||
            (std::abs(obj1 - best_obj1) < 1e-6 && objective + 1e-6 < best_objective)) {
            best_candidate.swap(candidate);
            best_obj1 = obj1;
            best_objective = objective;
            improved = true;
        }
    };

    struct TargetOption {
        Placement placement;
        int blocker_count = 0;
        int preference_loss = 0;
        int current_bay = 0;
    };

    const int best_pref = block.preferences.empty() ? 0 : *std::max_element(block.preferences.begin(), block.preferences.end());
    std::vector<int> bay_order(problem.bays.size());
    for (int bay_id = 0; bay_id < static_cast<int>(bay_order.size()); ++bay_id) {
        bay_order[bay_id] = bay_id;
    }
    std::sort(bay_order.begin(), bay_order.end(), [&](int a, int b) {
        const int pref_a = a < static_cast<int>(block.preferences.size()) ? block.preferences[a] : 0;
        const int pref_b = b < static_cast<int>(block.preferences.size()) ? block.preferences[b] : 0;
        if (pref_a != pref_b) {
            return pref_a > pref_b;
        }
        if ((a == current.bay_id) != (b == current.bay_id)) {
            return a == current.bay_id;
        }
        return a < b;
    });

    std::vector<TargetOption> target_options;
    std::set<std::tuple<int, int, int, int>> seen_target_options;
    auto add_target_option = [&](Placement placement) {
        if (placement.bay_id < 0 || placement.bay_id >= static_cast<int>(problem.bays.size()) ||
            placement.orient_idx < 0 || placement.orient_idx >= static_cast<int>(block.boxes.size())) {
            return;
        }
        if (!fits(problem.bays[placement.bay_id], block.boxes[placement.orient_idx], placement.x, placement.y)) {
            return;
        }
        const auto key = std::make_tuple(placement.bay_id, placement.orient_idx, placement.x, placement.y);
        if (!seen_target_options.insert(key).second) {
            return;
        }
        const int pref = placement.bay_id < static_cast<int>(block.preferences.size())
            ? block.preferences[placement.bay_id]
            : 0;
        target_options.push_back(
            TargetOption{
                placement,
                static_cast<int>(placement_access_blockers(problem, placement, without_target, 10).size()),
                best_pref - pref,
                placement.bay_id == current.bay_id ? 0 : 1,
            }
        );
    };

    Placement current_slot = current;
    current_slot.entry = ideal_entry;
    current_slot.exit = ideal_entry + block.processing;
    add_target_option(current_slot);

    for (int bay_id : bay_order) {
        if (timer.expired(180) || static_cast<int>(target_options.size()) >= 48) {
            break;
        }
        for (int orient_idx = 0; orient_idx < static_cast<int>(block.boxes.size()) && !timer.expired(180); ++orient_idx) {
            std::vector<std::pair<int, int>> positions =
                candidate_positions(problem, bay_id, block.boxes[orient_idx], without_target, 3);
            if (bay_id == current.bay_id && orient_idx == current.orient_idx &&
                fits(problem.bays[bay_id], block.boxes[orient_idx], current.x, current.y)) {
                positions.insert(positions.begin(), {current.x, current.y});
            }
            int checked_positions = 0;
            for (const auto& [x, y] : positions) {
                if (timer.expired(180) || ++checked_positions > 10) {
                    break;
                }
                Placement option;
                option.block_id = target_id;
                option.bay_id = bay_id;
                option.orient_idx = orient_idx;
                option.x = x;
                option.y = y;
                option.entry = ideal_entry;
                option.exit = ideal_entry + block.processing;
                option.workload = block.workload;
                add_target_option(option);
                if (static_cast<int>(target_options.size()) >= 48) {
                    break;
                }
            }
            if (static_cast<int>(target_options.size()) >= 48) {
                break;
            }
        }
    }
    if (target_options.empty()) {
        return false;
    }

    std::sort(target_options.begin(), target_options.end(), [&](const TargetOption& a, const TargetOption& b) {
        const int a_access = access_time_feasible_against_all(problem, a.placement, without_target) ? 0 : 1;
        const int b_access = access_time_feasible_against_all(problem, b.placement, without_target) ? 0 : 1;
        if (a_access != b_access) {
            return a_access < b_access;
        }
        if (a.blocker_count != b.blocker_count) {
            return a.blocker_count < b.blocker_count;
        }
        if (a.preference_loss != b.preference_loss) {
            return a.preference_loss < b.preference_loss;
        }
        if (a.current_bay != b.current_bay) {
            return a.current_bay < b.current_bay;
        }
        if (a.placement.bay_id != b.placement.bay_id) {
            return a.placement.bay_id < b.placement.bay_id;
        }
        if (a.placement.orient_idx != b.placement.orient_idx) {
            return a.placement.orient_idx < b.placement.orient_idx;
        }
        if (a.placement.y != b.placement.y) {
            return a.placement.y < b.placement.y;
        }
        return a.placement.x < b.placement.x;
    });

    const int option_limit = std::min<int>(static_cast<int>(target_options.size()), 6);
    for (int option_idx = 0; option_idx < option_limit && !timer.expired(180); ++option_idx) {
        const Placement ideal_target = target_options[option_idx].placement;
        std::vector<int> blockers = placement_access_blockers(problem, ideal_target, without_target, 10);
        blockers = fill_unique_blocks(blockers, time_window_blockers_for_target(problem, incumbent, target_id, 8), 14);
        blockers = fill_unique_blocks(blockers, entry_blockers_for_target(problem, incumbent, target_id, 8), 16);

        if (blockers.empty()) {
            if (!access_time_feasible_against_all(problem, ideal_target, without_target) &&
                !relaxed_time_feasible_against_all(problem, ideal_target, without_target)) {
                continue;
            }
            std::vector<Placement> direct = without_target;
            direct.push_back(ideal_target);
            left_shift_obj1_pass(problem, direct, timer);
            relaxed_left_shift_obj1_pass(problem, direct, timer);
            obj1_reschedule_pass(problem, direct, timer);
            consider(std::move(direct));
            continue;
        }

        const int prefix = std::min<int>(static_cast<int>(blockers.size()), 5 + ((iteration + option_idx) % 4));
        std::vector<int> removed{target_id};
        removed.insert(removed.end(), blockers.begin(), blockers.begin() + prefix);
        std::sort(removed.begin(), removed.end());
        removed.erase(std::unique(removed.begin(), removed.end()), removed.end());

        std::vector<Placement> base = remove_blocks(incumbent, removed);
        if (!placement_time_feasible_against_all(problem, ideal_target, base) &&
            !relaxed_time_feasible_against_all(problem, ideal_target, base)) {
            continue;
        }
        base.push_back(ideal_target);

        std::vector<int> blocker_sequence;
        blocker_sequence.reserve(removed.size());
        for (int block_id : removed) {
            if (block_id != target_id) {
                blocker_sequence.push_back(block_id);
            }
        }
        std::vector<std::vector<int>> sequences;
        sequences.push_back(due_order_sequence(problem, blocker_sequence));
        sequences.push_back(current_entry_sequence(problem, incumbent, blocker_sequence));
        sequences.push_back(tardy_first_sequence(problem, incumbent, blocker_sequence));

        for (const std::vector<int>& sequence : sequences) {
            if (timer.expired(180)) {
                break;
            }
            for (int bay_offset = 0; bay_offset < 2 && !timer.expired(180); ++bay_offset) {
                for (int pos_offset = 0; pos_offset < 2 && !timer.expired(180); ++pos_offset) {
                    std::vector<Placement> candidate = base;
                    if (!repair_sequence_obj1(
                            problem,
                            candidate,
                            sequence,
                            timer,
                            (bay_start + bay_offset) % 4,
                            (pos_start + pos_offset) % 3
                        )) {
                        continue;
                    }
                    left_shift_obj1_pass(problem, candidate, timer);
                    relaxed_left_shift_obj1_pass(problem, candidate, timer);
                    obj1_reschedule_pass(problem, candidate, timer);
                    consider(std::move(candidate));
                }
            }
        }
        if (improved) {
            break;
        }
    }

    if (improved) {
        incumbent.swap(best_candidate);
    }
    return improved;
}

bool alns_repair_iteration(
    const Problem& problem,
    std::vector<Placement>& incumbent,
    const Timer& timer,
    int iteration,
    Rng& rng
) {
    const double incumbent_objective = solution_objective(problem, incumbent);
    std::vector<int> removed = alns_removal_set(problem, incumbent, iteration, rng);
    if (removed.empty()) {
        return false;
    }

    std::vector<std::vector<int>> sequences;
    sequences.push_back(removed);
    sequences.push_back(due_order_sequence(problem, removed));
    sequences.push_back(current_entry_sequence(problem, incumbent, removed));
    sequences.push_back(tardy_first_sequence(problem, incumbent, removed));
    std::reverse(removed.begin(), removed.end());
    sequences.push_back(removed);

    bool improved = false;
    double best_objective = incumbent_objective;
    std::vector<Placement> best_candidate = incumbent;
    const int bay_start = iteration % 4;
    const int pos_start = (iteration / 4) % 3;
    for (const std::vector<int>& sequence : sequences) {
        if (timer.expired(120)) {
            break;
        }
        for (int bay_offset = 0; bay_offset < 3 && !timer.expired(120); ++bay_offset) {
            for (int pos_offset = 0; pos_offset < 3 && !timer.expired(120); ++pos_offset) {
                const int bay_mode = (bay_start + bay_offset) % 4;
                const int position_mode = (pos_start + pos_offset) % 3;
                std::vector<Placement> candidate = remove_blocks(incumbent, sequence);
                if (!repair_sequence(problem, candidate, sequence, timer, bay_mode, position_mode)) {
                    continue;
                }
                reschedule_pass(problem, candidate, timer);
                const double objective = solution_objective(problem, candidate);
                if (objective + 1e-6 < best_objective) {
                    best_candidate.swap(candidate);
                    best_objective = objective;
                    improved = true;
                }
            }
        }
    }
    if (improved) {
        incumbent.swap(best_candidate);
    }
    return improved;
}

bool alns_neighbor_iteration(
    const Problem& problem,
    const std::vector<Placement>& incumbent,
    std::vector<Placement>& neighbor,
    const Timer& timer,
    int iteration,
    Rng& rng
) {
    std::vector<int> removed = alns_removal_set(problem, incumbent, iteration, rng);
    if (removed.empty()) {
        return false;
    }

    std::vector<std::vector<int>> sequences;
    sequences.push_back(removed);
    sequences.push_back(due_order_sequence(problem, removed));
    sequences.push_back(current_entry_sequence(problem, incumbent, removed));
    sequences.push_back(tardy_first_sequence(problem, incumbent, removed));
    std::reverse(removed.begin(), removed.end());
    sequences.push_back(removed);

    bool generated = false;
    double best_obj1 = std::numeric_limits<double>::infinity();
    double best_objective = std::numeric_limits<double>::infinity();
    std::vector<Placement> best_candidate;
    const int bay_start = iteration % 4;
    const int pos_start = (iteration / 4) % 3;
    for (const std::vector<int>& sequence : sequences) {
        if (timer.expired(120)) {
            break;
        }
        for (int bay_offset = 0; bay_offset < 3 && !timer.expired(120); ++bay_offset) {
            for (int pos_offset = 0; pos_offset < 3 && !timer.expired(120); ++pos_offset) {
                const int bay_mode = (bay_start + bay_offset) % 4;
                const int position_mode = (pos_start + pos_offset) % 3;
                std::vector<Placement> candidate = remove_blocks(incumbent, sequence);
                if (!repair_sequence(problem, candidate, sequence, timer, bay_mode, position_mode)) {
                    continue;
                }
                reschedule_pass(problem, candidate, timer);
                left_shift_obj1_pass(problem, candidate, timer);
                relaxed_left_shift_obj1_pass(problem, candidate, timer);
                obj1_reschedule_pass(problem, candidate, timer);
                if (candidate.size() != problem.blocks.size()) {
                    continue;
                }
                std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                    return a.block_id < b.block_id;
                });
                const double obj1 = solution_obj1(problem, candidate);
                const double objective = solution_objective(problem, candidate);
                if (!generated || obj1 + 1e-6 < best_obj1 ||
                    (std::abs(obj1 - best_obj1) < 1e-6 && objective + 1e-6 < best_objective)) {
                    best_candidate.swap(candidate);
                    best_obj1 = obj1;
                    best_objective = objective;
                    generated = true;
                }
            }
        }
    }
    if (!generated) {
        return false;
    }
    neighbor.swap(best_candidate);
    return true;
}

bool accept_worse_alns_move(
    const Problem& problem,
    double current_obj1,
    double current_objective,
    double candidate_obj1,
    double candidate_objective,
    double best_obj1,
    int local_iter,
    int max_iterations,
    Rng& rng
) {
    const double obj1_slack = std::max(8.0, best_obj1 * 0.04);
    if (candidate_obj1 > best_obj1 + obj1_slack) {
        return false;
    }
    const double objective_slack = std::max(100000.0, std::abs(current_objective) * 0.08);
    if (candidate_objective > current_objective + objective_slack) {
        return false;
    }
    const double progress =
        max_iterations <= 1 ? 1.0 : std::min(1.0, static_cast<double>(local_iter) / static_cast<double>(max_iterations - 1));
    const double temperature = std::max(1.5, (best_obj1 > 1000.0 ? 80.0 : 18.0) * (1.0 - progress));
    const double delta_obj1 = std::max(0.0, candidate_obj1 - current_obj1);
    const double objective_scale = std::max(1.0, std::abs(current_objective) * 0.015);
    const double delta_objective = std::max(0.0, candidate_objective - current_objective) / objective_scale;
    const double score_delta = delta_obj1 + delta_objective;
    const double probability = std::exp(-score_delta / temperature);
    return rng.next_double() < probability;
}

void add_to_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    std::vector<Placement> candidate,
    int max_size
);

bool obj1_first_repair_once(
    const Problem& problem,
    std::vector<Placement>& incumbent,
    const Timer& timer,
    int iteration
) {
    const double incumbent_obj1 = solution_obj1(problem, incumbent);
    if (incumbent_obj1 <= 0.0 || timer.expired(180)) {
        return false;
    }
    const double incumbent_objective = solution_objective(problem, incumbent);
    std::vector<Placement> best_candidate = incumbent;
    double best_obj1 = incumbent_obj1;
    double best_objective = incumbent_objective;
    bool improved = false;
    auto consider_candidate = [&](const std::vector<Placement>& candidate) {
        const double candidate_obj1 = solution_obj1(problem, candidate);
        const double candidate_objective = solution_objective(problem, candidate);
        if (candidate_obj1 + 1e-6 < best_obj1 ||
            (std::abs(candidate_obj1 - best_obj1) < 1e-6 &&
             candidate_objective + 1e-6 < best_objective)) {
            best_candidate = candidate;
            best_obj1 = candidate_obj1;
            best_objective = candidate_objective;
            improved = true;
        }
    };

    std::vector<int> targets = obj1_repair_targets(problem, incumbent, 30);
    targets = fill_unique_blocks(targets, large_tardy_entry_targets(problem, incumbent, 30), 45);
    const int bay_start = iteration % 4;
    const int pos_start = (iteration / 4) % 3;
    for (int target_id : targets) {
        if (timer.expired(180)) {
            break;
        }
        const std::vector<int> entry_blockers = entry_blockers_for_target(problem, incumbent, target_id, 9);
        const std::vector<int> exit_blockers = exit_blockers_for_target(problem, incumbent, target_id, 7);

        std::vector<std::vector<int>> clusters;
        auto add_cluster = [&](std::vector<int> cluster) {
            std::sort(cluster.begin(), cluster.end());
            cluster.erase(std::unique(cluster.begin(), cluster.end()), cluster.end());
            if (std::find(cluster.begin(), cluster.end(), target_id) == cluster.end()) {
                cluster.insert(cluster.begin(), target_id);
            }
            for (const auto& existing : clusters) {
                if (existing == cluster) {
                    return;
                }
            }
            clusters.push_back(std::move(cluster));
        };

        for (int blocker_id : entry_blockers) {
            add_cluster({target_id, blocker_id});
        }
        for (int count = 2; count <= std::min<int>(4, entry_blockers.size()); ++count) {
            std::vector<int> cluster{target_id};
            cluster.insert(cluster.end(), entry_blockers.begin(), entry_blockers.begin() + count);
            add_cluster(std::move(cluster));
        }
        std::vector<int> full_cluster{target_id};
        full_cluster = fill_unique_blocks(full_cluster, entry_blockers, 10);
        full_cluster = fill_unique_blocks(full_cluster, exit_blockers, 12);
        if (full_cluster.size() == 1) {
            full_cluster = fill_unique_blocks(full_cluster, pair_partners(problem, incumbent, target_id, 4), 5);
        }
        add_cluster(std::move(full_cluster));

        for (const std::vector<int>& cluster : clusters) {
            if (timer.expired(180)) {
                break;
            }
            std::vector<std::vector<int>> sequences;
            sequences.push_back(due_order_sequence(problem, cluster));
            sequences.push_back(tardy_first_sequence(problem, incumbent, cluster));
            sequences.push_back(current_entry_sequence(problem, incumbent, cluster));
            std::vector<int> target_first = cluster;
            std::stable_sort(target_first.begin(), target_first.end(), [&](int a, int b) {
                if (a == b) {
                    return false;
                }
                if (a == target_id) {
                    return true;
                }
                if (b == target_id) {
                    return false;
                }
                return problem.blocks[a].due < problem.blocks[b].due;
            });
            sequences.push_back(std::move(target_first));

            for (const std::vector<int>& sequence : sequences) {
                if (timer.expired(180)) {
                    break;
                }
                for (int bay_offset = 0; bay_offset < 4 && !timer.expired(180); ++bay_offset) {
                    for (int pos_offset = 0; pos_offset < 3 && !timer.expired(180); ++pos_offset) {
                        const int bay_mode = (bay_start + bay_offset) % 4;
                        const int position_mode = (pos_start + pos_offset) % 3;
                        for (int repair_mode = 0; repair_mode < 2 && !timer.expired(180); ++repair_mode) {
                            std::vector<Placement> candidate = remove_blocks(incumbent, sequence);
                            const bool repaired = repair_mode == 0
                                ? repair_sequence_obj1(problem, candidate, sequence, timer, bay_mode, position_mode)
                                : repair_sequence(problem, candidate, sequence, timer, bay_mode, position_mode);
                            if (!repaired) {
                                continue;
                            }
                            consider_candidate(candidate);
                            left_shift_obj1_pass(problem, candidate, timer);
                            consider_candidate(candidate);
                            reschedule_pass(problem, candidate, timer);
                            left_shift_obj1_pass(problem, candidate, timer);
                            consider_candidate(candidate);
                        }
                    }
                }
            }
        }
    }

    if (improved) {
        incumbent.swap(best_candidate);
    }
    return improved;
}

void obj1_first_repair_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size
) {
    if (pool.empty() || timer.expired(800)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });

    const int seed_count = std::min<int>(static_cast<int>(pool.size()), max_size);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    int global_iteration = 0;
    for (auto current : seeds) {
        if (timer.expired(800)) {
            break;
        }
        left_shift_obj1_pass(problem, current, timer);
        relaxed_left_shift_obj1_pass(problem, current, timer);
        obj1_reschedule_pass(problem, current, timer);
        add_to_pool(problem, pool, current, max_size);
        int stale = 0;
        while (!timer.expired(800) && stale < 18 && solution_obj1(problem, current) > 0.0) {
            const double before_obj1 = solution_obj1(problem, current);
            bool improved = python_style_obj1_polish_once(problem, current, timer, global_iteration);
            if (!improved) {
                improved = protect_tardy_entry_slot_once(problem, current, timer, global_iteration);
            }
            if (!improved) {
                improved = obj1_first_repair_once(problem, current, timer, global_iteration);
            }
            ++global_iteration;
            if (improved && solution_obj1(problem, current) + 1e-6 < before_obj1) {
                stale = 0;
                add_to_pool(problem, pool, current, max_size);
            } else {
                ++stale;
            }
        }
        add_to_pool(problem, pool, std::move(current), max_size);
    }
}

void pair_relocate_pass(
    const Problem& problem,
    std::vector<Placement>& best,
    const Timer& timer,
    int bay_mode,
    int position_mode
) {
    double best_objective = solution_objective(problem, best);
    const int max_targets = std::min<int>(static_cast<int>(best.size()), 45);
    for (int target_id : relocation_order(problem, best, max_targets)) {
        if (timer.expired(120)) {
            break;
        }
        const Block& target_block = problem.blocks[target_id];
        const Placement target = find_placement(best, target_id);
        if (std::max(0, target.exit - target_block.due) <= 0) {
            continue;
        }
        for (int partner_id : pair_partners(problem, best, target_id, 10)) {
            if (timer.expired(120)) {
                break;
            }
            for (int order_variant = 0; order_variant < 2; ++order_variant) {
                std::vector<Placement> candidate = remove_block(remove_block(best, target_id), partner_id);
                std::vector<int> sequence;
                if (order_variant == 0) {
                    sequence = {target_id, partner_id};
                } else {
                    sequence = {partner_id, target_id};
                }
                if (!insert_block_sequence(problem, candidate, sequence, timer, bay_mode, position_mode)) {
                    continue;
                }
                reschedule_pass(problem, candidate, timer);
                const double objective = solution_objective(problem, candidate);
                if (objective + 1e-6 < best_objective) {
                    best.swap(candidate);
                    best_objective = objective;
                }
            }
        }
    }
}

void add_to_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    std::vector<Placement> candidate,
    int max_size
) {
    if (candidate.size() != problem.blocks.size()) {
        return;
    }
    std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });
    if (g_filter_feasible_like && !solution_feasible_like(problem, candidate)) {
        return;
    }
    const double objective = solution_objective(problem, candidate);
    for (const auto& existing : pool) {
        if (std::abs(solution_objective(problem, existing) - objective) < 1e-6) {
            bool same = true;
            for (size_t i = 0; i < existing.size(); ++i) {
                if (existing[i].bay_id != candidate[i].bay_id || existing[i].x != candidate[i].x ||
                    existing[i].y != candidate[i].y || existing[i].orient_idx != candidate[i].orient_idx ||
                    existing[i].entry != candidate[i].entry || existing[i].exit != candidate[i].exit) {
                    same = false;
                    break;
                }
            }
            if (same) {
                return;
            }
        }
    }
    pool.push_back(std::move(candidate));
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    if (static_cast<int>(pool.size()) > max_size) {
        pool.resize(max_size);
    }
}

bool problem_has_polygon_payload(const Problem& problem) {
    for (const Block& block : problem.blocks) {
        for (const auto& orientation_layers : block.layer_polygons) {
            if (!orientation_layers.empty()) {
                return true;
            }
        }
    }
    return false;
}

void same_position_shift_sample_pool(
    const Problem& problem,
    const std::vector<std::vector<Placement>>& seed_sources,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    std::vector<std::vector<Placement>>& forced_outputs
) {
    if (seed_sources.empty() || timer.expired(120)) {
        return;
    }
    std::vector<std::vector<Placement>> seeds = seed_sources;
    std::sort(seeds.begin(), seeds.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    const bool focused_late_shift = std::getenv("OGC_CPP_FOCUSED_LATE_SHIFT") != nullptr;
    const int seed_count = std::min<int>(static_cast<int>(seeds.size()), 4);
    for (int seed_idx = 0; seed_idx < seed_count && !timer.expired(120); ++seed_idx) {
        const std::vector<Placement> seed = seeds[seed_idx];
        const double seed_obj1 = solution_obj1(problem, seed);
        if (seed_obj1 <= 1000.0) {
            continue;
        }
        std::vector<int> early_due_targets = obj1_repair_targets(problem, seed, static_cast<int>(problem.blocks.size()));
        early_due_targets = due_order_sequence(problem, std::move(early_due_targets));
        std::vector<int> targets = fill_unique_blocks({}, early_due_targets, 140);
        targets = fill_unique_blocks(std::move(targets), obj1_repair_targets(problem, seed, 96), 220);
        targets = fill_unique_blocks(std::move(targets), early_due_targets, 240);
        for (int target_idx = 0; target_idx < static_cast<int>(targets.size()); ++target_idx) {
            const int block_id = targets[target_idx];
            if (timer.expired(120) || static_cast<int>(forced_outputs.size()) >= 1500) {
                return;
            }
            const Placement current = find_placement(seed, block_id);
            if (current.block_id != block_id) {
                continue;
            }
            const Block& block = problem.blocks[block_id];
            const int latest = current.entry - 1;
            if (latest < block.release) {
                continue;
            }
            std::vector<int> starts;
            auto push_start = [&](int start) {
                if (start < block.release || start >= current.entry) {
                    return;
                }
                if (std::find(starts.begin(), starts.end(), start) == starts.end()) {
                    starts.push_back(start);
                }
            };
            const bool late_large_tardy_seed = focused_late_shift && seed_obj1 <= 6610.0;
            if (late_large_tardy_seed) {
                const int focused_deltas[] = {12, 14, 15, 17, 10, 8, 18, 16, 13, 11, 20};
                for (int delta : focused_deltas) {
                    push_start(current.entry - delta);
                }
            } else {
                push_start(block.release);
                push_start(std::max(block.release, block.due - block.processing));
                const int coarse_deltas[] = {36, 17, 15, 14, 12, 3, 2, 31, 24, 18, 10, 8, 6, 5, 4, 1};
                for (int delta : coarse_deltas) {
                    push_start(current.entry - delta);
                }
                push_start(latest);
            }
            if (!late_large_tardy_seed && target_idx < 48) {
                for (int delta = 1; delta <= 45; ++delta) {
                    push_start(current.entry - delta);
                }
            }
            int emitted_for_block = 0;
            const int emit_limit = late_large_tardy_seed ? 2 : (target_idx < 48 ? 18 : 6);
            for (int start : starts) {
                if (timer.expired(120) || emitted_for_block >= emit_limit) {
                    break;
                }
                if (start < block.release || start >= current.entry) {
                    continue;
                }
                Placement shifted = current;
                shifted.entry = start;
                shifted.exit = start + block.processing;
                std::vector<Placement> candidate = seed;
                bool replaced = false;
                for (Placement& placement : candidate) {
                    if (placement.block_id == block_id) {
                        placement = shifted;
                        replaced = true;
                        break;
                    }
                }
                if (!replaced || solution_obj1(problem, candidate) + 1e-6 >= seed_obj1) {
                    continue;
                }
                std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                    return a.block_id < b.block_id;
                });
                forced_outputs.push_back(candidate);
                ++emitted_for_block;
            }
        }
    }
}

void high_w1_small_tail_cluster_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    std::vector<std::vector<Placement>>& forced_outputs
) {
    if (pool.empty() || problem.w1 < 1000.0 || timer.expired(900)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 4);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    for (const std::vector<Placement>& seed : seeds) {
        if (timer.expired(900)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        const double small_tail_limit = problem.blocks.size() <= 140 ? 60.0 : 10.0;
        if (seed_obj1 <= 0.0 || seed_obj1 > small_tail_limit) {
            continue;
        }
        std::vector<int> cluster = obj1_repair_targets(problem, seed, 16);
        const std::vector<int> tardy_targets = cluster;
        for (int target_id : tardy_targets) {
            cluster = fill_unique_blocks(cluster, entry_blockers_for_target(problem, seed, target_id, 8), 54);
            cluster = fill_unique_blocks(cluster, time_window_blockers_for_target(problem, seed, target_id, 10), 72);
            cluster = fill_unique_blocks(cluster, pair_partners(problem, seed, target_id, 4), 84);
            if (problem.blocks.size() <= 140 && problem.bays.size() <= 3) {
                for (int bay_id = 0; bay_id < static_cast<int>(problem.bays.size()); ++bay_id) {
                    cluster = fill_unique_blocks(
                        cluster,
                        bay_blocks_near_tardy(problem, seed, bay_id, target_id, 14),
                        124
                    );
                }
            }
        }
        if (cluster.size() <= tardy_targets.size()) {
            continue;
        }
        std::vector<std::vector<int>> sequences;
        sequences.push_back(due_order_sequence(problem, cluster));
        sequences.push_back(release_due_sequence(problem, cluster));
        sequences.push_back(tardy_first_sequence(problem, seed, cluster));
        sequences.push_back(current_entry_sequence(problem, seed, cluster));
        std::vector<int> reversed_current = current_entry_sequence(problem, seed, cluster);
        std::reverse(reversed_current.begin(), reversed_current.end());
        sequences.push_back(std::move(reversed_current));

        for (const std::vector<int>& sequence : sequences) {
            if (timer.expired(900)) {
                break;
            }
            for (int bay_mode = 0; bay_mode < 4 && !timer.expired(900); ++bay_mode) {
                for (int position_mode = 0; position_mode < 4 && !timer.expired(900); ++position_mode) {
                    for (int repair_mode = 0; repair_mode < 2 && !timer.expired(900); ++repair_mode) {
                        std::vector<Placement> candidate = remove_blocks(seed, sequence);
                        const bool repaired = repair_mode == 0
                            ? repair_sequence_obj1(problem, candidate, sequence, timer, bay_mode, position_mode)
                            : repair_sequence(problem, candidate, sequence, timer, bay_mode, position_mode);
                        if (!repaired) {
                            continue;
                        }
                        left_shift_obj1_pass(problem, candidate, timer);
                        obj1_reschedule_pass(problem, candidate, timer);
                        if (solution_obj1(problem, candidate) + 1e-6 >= seed_obj1) {
                            continue;
                        }
                        std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                            return a.block_id < b.block_id;
                        });
                        forced_outputs.push_back(candidate);
                        add_to_pool(problem, pool, std::move(candidate), max_size);
                        if (static_cast<int>(forced_outputs.size()) >= 160) {
                            return;
                        }
                    }
                }
            }
        }
    }
}

void high_w1_small_tail_option_repack_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    std::vector<std::vector<Placement>>& forced_outputs
) {
    if (pool.empty() || problem.w1 < 1000.0 || problem.blocks.size() > 140 || timer.expired(900)) {
        return;
    }
    const bool debug_small_tail = std::getenv("OGC_CPP_DEBUG_SMALL_TAIL") != nullptr;
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 4);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    for (const std::vector<Placement>& seed : seeds) {
        if (timer.expired(900)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        const bool wide_three_bay_medium_tail =
            problem.bays.size() >= 3 && problem.w3 <= 200.0;
        const bool wide_two_bay_medium_tail =
            problem.bays.size() == 2 && problem.w3 <= 200.0;
        const bool narrow_late_three_bay_tail =
            problem.bays.size() >= 3 && problem.w3 >= 250.0 && seed_obj1 <= 50.0;
        const double max_option_repack_obj1 =
            problem.bays.size() >= 3
                ? (wide_three_bay_medium_tail ? 160.0 : 80.0)
                : (wide_two_bay_medium_tail ? 1000.0 : 3.0);
        if (seed_obj1 <= 0.0 || seed_obj1 > max_option_repack_obj1) {
            continue;
        }
        if (debug_small_tail) {
            std::cerr << "small_tail seed_obj1=" << seed_obj1 << " objective="
                      << solution_objective(problem, seed) << "\n";
        }
        const double seed_objective = solution_objective(problem, seed);
        std::vector<int> targets =
            obj1_repair_targets(
                problem,
                seed,
                (wide_three_bay_medium_tail || wide_two_bay_medium_tail || narrow_late_three_bay_tail) ? 12 : 4
            );
        if (narrow_late_three_bay_tail || wide_two_bay_medium_tail) {
            std::stable_sort(targets.begin(), targets.end(), [&](int a, int b) {
                const Placement pa = find_placement(seed, a);
                const Placement pb = find_placement(seed, b);
                const int tardy_a = std::max(0, pa.exit - problem.blocks[a].due);
                const int tardy_b = std::max(0, pb.exit - problem.blocks[b].due);
                if (narrow_late_three_bay_tail && tardy_a != tardy_b) {
                    return tardy_a > tardy_b;
                }
                if (tardy_a != tardy_b) {
                    return tardy_a < tardy_b;
                }
                if (problem.blocks[a].due != problem.blocks[b].due) {
                    return problem.blocks[a].due < problem.blocks[b].due;
                }
                return a < b;
            });
        }
        for (int target_id : targets) {
            if (timer.expired(900)) {
                break;
            }
            const Placement current_target = find_placement(seed, target_id);
            const Block& target_block = problem.blocks[target_id];
            if (std::max(0, current_target.exit - target_block.due) <= 0) {
                continue;
            }
            if (debug_small_tail) {
                std::cerr << " target=" << target_id << " tardy="
                          << std::max(0, current_target.exit - target_block.due) << "\n";
            }

            std::vector<int> ranked{target_id};
            ranked = fill_unique_blocks(ranked, entry_blockers_for_target(problem, seed, target_id, 10), 16);
            ranked = fill_unique_blocks(ranked, time_window_blockers_for_target(problem, seed, target_id, 12), 20);
            for (int bay_id = 0; bay_id < static_cast<int>(problem.bays.size()); ++bay_id) {
                ranked = fill_unique_blocks(
                    ranked,
                    bay_blocks_near_tardy(problem, seed, bay_id, target_id, 20),
                    32
                );
            }
            ranked = fill_unique_blocks(ranked, pair_partners(problem, seed, target_id, 10), 36);
            if (debug_small_tail) {
                std::cerr << " ranked";
                for (int id : ranked) {
                    std::cerr << ' ' << id;
                }
                std::cerr << "\n";
            }

            std::vector<std::vector<int>> candidate_subsets;
            auto push_subset = [&](std::vector<int> selected, int max_count) {
                selected = fill_unique_blocks(std::move(selected), std::vector<int>{target_id}, max_count);
                std::sort(selected.begin(), selected.end());
                selected.erase(std::unique(selected.begin(), selected.end()), selected.end());
                const size_t min_subset_size =
                    (narrow_late_three_bay_tail || wide_two_bay_medium_tail) ? 1U : 2U;
                if (selected.size() >= min_subset_size && selected.size() <= 14) {
                    candidate_subsets.push_back(std::move(selected));
                }
            };
            if (narrow_late_three_bay_tail || wide_two_bay_medium_tail) {
                push_subset(std::vector<int>{target_id}, 1);
            }
            if (!wide_two_bay_medium_tail) {
                const std::vector<int> direct_blockers = entry_blockers_for_target(problem, seed, target_id, 4);
                for (int blocker_id : direct_blockers) {
                    const Placement blocker = find_placement(seed, blocker_id);
                    std::vector<int> cross_overlap;
                    for (int candidate_id : time_overlap_blocks_for_target(problem, seed, blocker_id, 16)) {
                        if (find_placement(seed, candidate_id).bay_id != blocker.bay_id) {
                            cross_overlap.push_back(candidate_id);
                        }
                        if (static_cast<int>(cross_overlap.size()) >= 6) {
                            break;
                        }
                    }
                    std::sort(cross_overlap.begin(), cross_overlap.end(), [&](int a, int b) {
                        const Placement placement_a = find_placement(seed, a);
                        const Placement placement_b = find_placement(seed, b);
                        const int a_entry_delta = std::abs(placement_a.entry - blocker.entry);
                        const int b_entry_delta = std::abs(placement_b.entry - blocker.entry);
                        if (a_entry_delta != b_entry_delta) {
                            return a_entry_delta < b_entry_delta;
                        }
                        const Block& block_a = problem.blocks[a];
                        const Block& block_b = problem.blocks[b];
                        if (block_a.due != block_b.due) {
                            return block_a.due < block_b.due;
                        }
                        return a < b;
                    });
                    std::vector<int> window_blocks =
                        time_window_blockers_for_target(problem, seed, target_id, 12);
                    std::sort(window_blocks.begin(), window_blocks.end(), [&](int a, int b) {
                        const Block& block_a = problem.blocks[a];
                        const Block& block_b = problem.blocks[b];
                        if (block_a.due != block_b.due) {
                            return block_a.due < block_b.due;
                        }
                        return a < b;
                    });
                    for (int i = 0; i < static_cast<int>(cross_overlap.size()); ++i) {
                        for (int j = i + 1; j < static_cast<int>(cross_overlap.size()); ++j) {
                            for (int window_id : window_blocks) {
                                std::vector<int> compact{
                                    target_id,
                                    blocker_id,
                                    cross_overlap[i],
                                    cross_overlap[j],
                                    window_id
                                };
                                push_subset(std::move(compact), 5);
                            }
                        }
                    }

                    std::vector<int> overlap_focused{target_id, blocker_id};
                    overlap_focused =
                        fill_unique_blocks(overlap_focused, time_overlap_blocks_for_target(problem, seed, blocker_id, 10), 10);
                    overlap_focused =
                        fill_unique_blocks(overlap_focused, time_window_blockers_for_target(problem, seed, target_id, 8), 14);
                    push_subset(std::move(overlap_focused), 14);

                    std::vector<int> window_focused{target_id, blocker_id};
                    window_focused =
                        fill_unique_blocks(window_focused, time_window_blockers_for_target(problem, seed, target_id, 10), 10);
                    window_focused =
                        fill_unique_blocks(window_focused, time_overlap_blocks_for_target(problem, seed, blocker_id, 6), 14);
                    push_subset(std::move(window_focused), 14);
                }
                const std::vector<int> subset_sizes{5, 6, 8, 10};
                for (int subset_size : subset_sizes) {
                    if (timer.expired(900) || static_cast<int>(ranked.size()) < 2) {
                        break;
                    }
                    std::vector<int> selected(
                        ranked.begin(),
                        ranked.begin() + std::min<int>(subset_size, static_cast<int>(ranked.size()))
                    );
                    push_subset(std::move(selected), subset_size);
                }
            }
            for (const std::vector<int>& selected : candidate_subsets) {
                if (timer.expired(900)) {
                    continue;
                }
                if (debug_small_tail) {
                    std::cerr << " selected";
                    for (int id : selected) {
                        std::cerr << ' ' << id;
                    }
                    std::cerr << "\n";
                }

                std::vector<Placement> fixed = remove_blocks(seed, selected);
                std::vector<std::vector<Placement>> options_by_block(selected.size());
                bool missing_options = false;
                for (size_t idx = 0; idx < selected.size(); ++idx) {
                    const int block_id = selected[idx];
                    const Block& block = problem.blocks[block_id];
                    const Placement original = find_placement(seed, block_id);
                    const int current_tardy = std::max(0, original.exit - block.due);
                    int latest_start = block.due - block.processing;
                    if ((wide_three_bay_medium_tail || wide_two_bay_medium_tail) &&
                        seed_obj1 > 80.0 && current_tardy > 0) {
                        latest_start = std::max(latest_start, original.entry - 1);
                    }
                    if (narrow_late_three_bay_tail && current_tardy > 0 && current_tardy <= 3) {
                        latest_start = std::max(latest_start, original.entry - 1);
                    }
                    if (latest_start < block.release) {
                        missing_options = true;
                        break;
                    }
                    std::vector<Placement> options;
                    std::set<std::tuple<int, int, int, int, int>> seen_options;
                    auto add_option = [&](int bay_id, int orient_idx, int x, int y, int start) {
                        if (timer.expired(900)) {
                            return;
                        }
                        if (start < block.release || start > latest_start) {
                            return;
                        }
                        if (bay_id < 0 || bay_id >= static_cast<int>(problem.bays.size()) ||
                            orient_idx < 0 || orient_idx >= static_cast<int>(block.boxes.size())) {
                            return;
                        }
                        if (!fits(problem.bays[bay_id], block.boxes[orient_idx], x, y)) {
                            return;
                        }
                        const auto key = std::make_tuple(bay_id, orient_idx, x, y, start);
                        if (seen_options.count(key)) {
                            return;
                        }
                        Placement option;
                        option.block_id = block_id;
                        option.bay_id = bay_id;
                        option.orient_idx = orient_idx;
                        option.x = x;
                        option.y = y;
                        option.entry = start;
                        option.exit = start + block.processing;
                        option.workload = block.workload;
                        if (!access_time_feasible_against_all(problem, option, fixed)) {
                            return;
                        }
                        seen_options.insert(key);
                        options.push_back(option);
                    };

                    for (int start = block.release; start <= latest_start; ++start) {
                        add_option(original.bay_id, original.orient_idx, original.x, original.y, start);
                    }
                    for (int bay_id = 0; bay_id < static_cast<int>(problem.bays.size()) && !timer.expired(900);
                         ++bay_id) {
                        for (int orient_idx = 0; orient_idx < static_cast<int>(block.boxes.size()) &&
                                                 !timer.expired(900);
                             ++orient_idx) {
                            const BBox& box = block.boxes[orient_idx];
                            const int min_x = lower_x(box);
                            const int max_x = std::max(
                                min_x,
                                static_cast<int>(std::floor(problem.bays[bay_id].width - box.max_x + 1e-9))
                            );
                            const int min_y = lower_y(box);
                            const int max_y = std::max(
                                min_y,
                                static_cast<int>(std::floor(problem.bays[bay_id].height - box.max_y + 1e-9))
                            );
                            std::vector<std::pair<int, int>> positions =
                                candidate_positions(problem, bay_id, block.boxes[orient_idx], fixed, 3);
                            for (const Placement& anchor : seed) {
                                if (anchor.bay_id != bay_id) {
                                    continue;
                                }
                                for (int dx : {-3, -1, 0, 1, 3}) {
                                    for (int dy : {-3, -1, 0, 1, 3}) {
                                        const int x = std::max(min_x, std::min(max_x, anchor.x + dx));
                                        const int y = std::max(min_y, std::min(max_y, anchor.y + dy));
                                        positions.push_back({x, y});
                                    }
                                }
                            }
                            for (int gx = min_x; gx <= max_x; gx += 3) {
                                for (int gy = min_y; gy <= max_y; gy += 3) {
                                    positions.push_back({gx, gy});
                                }
                                positions.push_back({gx, max_y});
                            }
                            for (int gy = min_y; gy <= max_y; gy += 3) {
                                positions.push_back({max_x, gy});
                            }
                            positions.push_back({min_x, min_y});
                            positions.push_back({min_x, max_y});
                            positions.push_back({max_x, min_y});
                            positions.push_back({max_x, max_y});
                            int checked_positions = 0;
                            for (const auto& [x, y] : positions) {
                                if (timer.expired(900) || ++checked_positions > 3500) {
                                    break;
                                }
                                for (int start = block.release; start <= latest_start && !timer.expired(900); ++start) {
                                    add_option(bay_id, orient_idx, x, y, start);
                                    if (static_cast<int>(options.size()) >= 180) {
                                        break;
                                    }
                                }
                                if (static_cast<int>(options.size()) >= 180) {
                                    break;
                                }
                            }
                        }
                    }
                    std::sort(options.begin(), options.end(), [&](const Placement& a, const Placement& b) {
                        if (narrow_late_three_bay_tail) {
                            const int tardy_a = std::max(0, a.exit - block.due);
                            const int tardy_b = std::max(0, b.exit - block.due);
                            if (tardy_a != tardy_b) {
                                return tardy_a < tardy_b;
                            }
                            if (a.entry != b.entry) {
                                return a.entry < b.entry;
                            }
                            if ((a.bay_id == original.bay_id) != (b.bay_id == original.bay_id)) {
                                return a.bay_id == original.bay_id;
                            }
                        }
                        const int best_pref = *std::max_element(block.preferences.begin(), block.preferences.end());
                        const int a_loss = best_pref - block.preferences[a.bay_id];
                        const int b_loss = best_pref - block.preferences[b.bay_id];
                        if (a_loss != b_loss) {
                            return a_loss < b_loss;
                        }
                        if ((a.bay_id == original.bay_id) != (b.bay_id == original.bay_id)) {
                            return a.bay_id == original.bay_id;
                        }
                        if (a.entry != b.entry) {
                            return a.entry < b.entry;
                        }
                        if (a.x != b.x) {
                            return a.x < b.x;
                        }
                        return a.y < b.y;
                    });
                    if (static_cast<int>(options.size()) > 240) {
                        std::vector<Placement> trimmed;
                        trimmed.reserve(240);
                        std::set<std::tuple<int, int, int, int, int>> kept;
                        auto keep_option = [&](const Placement& option) {
                            if (static_cast<int>(trimmed.size()) >= 240) {
                                return;
                            }
                            const auto key = std::make_tuple(
                                option.bay_id,
                                option.orient_idx,
                                option.x,
                                option.y,
                                option.entry
                            );
                            if (kept.insert(key).second) {
                                trimmed.push_back(option);
                            }
                        };
                        for (const Placement& option : options) {
                            keep_option(option);
                            if (static_cast<int>(trimmed.size()) >= 80) {
                                break;
                            }
                        }
                        std::set<std::tuple<int, int, int, int>> used_slots;
                        for (const Placement& option : options) {
                            if (static_cast<int>(trimmed.size()) >= 240) {
                                break;
                            }
                            const BBox& box = block.boxes[option.orient_idx];
                            const int min_x = lower_x(box);
                            const int max_x = std::max(
                                min_x,
                                static_cast<int>(std::floor(problem.bays[option.bay_id].width - box.max_x + 1e-9))
                            );
                            const int min_y = lower_y(box);
                            const int max_y = std::max(
                                min_y,
                                static_cast<int>(std::floor(problem.bays[option.bay_id].height - box.max_y + 1e-9))
                            );
                            const int x_bin = std::min(
                                11,
                                std::max(0, (option.x - min_x) * 12 / std::max(1, max_x - min_x + 1))
                            );
                            const int y_bin = std::min(
                                5,
                                std::max(0, (option.y - min_y) * 6 / std::max(1, max_y - min_y + 1))
                            );
                            const int time_bin = std::min(
                                3,
                                std::max(0, (option.entry - block.release) * 4 /
                                                std::max(1, latest_start - block.release + 1))
                            );
                            const auto slot_key =
                                std::make_tuple(option.bay_id, option.orient_idx, x_bin, y_bin * 4 + time_bin);
                            if (used_slots.insert(slot_key).second) {
                                keep_option(option);
                            }
                        }
                        for (const Placement& option : options) {
                            if (static_cast<int>(trimmed.size()) >= 240) {
                                break;
                            }
                            keep_option(option);
                        }
                        options.swap(trimmed);
                    }
                    if (options.empty()) {
                        missing_options = true;
                        break;
                    }
                    if (debug_small_tail) {
                        std::cerr << "  options block=" << block_id << " count=" << options.size() << "\n";
                    }
                    options_by_block[idx] = std::move(options);
                }
                if (missing_options || timer.expired(900)) {
                    if (debug_small_tail) {
                        std::cerr << "  missing_or_expired\n";
                    }
                    continue;
                }

                std::vector<int> order(selected.size());
                for (size_t i = 0; i < selected.size(); ++i) {
                    order[i] = static_cast<int>(i);
                }
                std::sort(order.begin(), order.end(), [&](int a, int b) {
                    if (options_by_block[a].size() != options_by_block[b].size()) {
                        return options_by_block[a].size() < options_by_block[b].size();
                    }
                    return problem.blocks[selected[a]].due < problem.blocks[selected[b]].due;
                });

                std::vector<Placement> chosen;
                chosen.reserve(selected.size());
                std::vector<std::vector<Placement>> local_candidates;
                int nodes = 0;
                const int node_limit = 50000;
                std::function<bool(int)> dfs = [&](int depth) -> bool {
                    if (timer.expired(900) || ++nodes > node_limit) {
                        return false;
                    }
                    if (depth == static_cast<int>(order.size())) {
                        std::vector<Placement> candidate = fixed;
                        candidate.insert(candidate.end(), chosen.begin(), chosen.end());
                        std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                            return a.block_id < b.block_id;
                        });
                        if (solution_obj1(problem, candidate) + 1e-6 >= seed_obj1) {
                            return false;
                        }
                        if (!solution_feasible_like(problem, candidate)) {
                            return false;
                        }
                        local_candidates.push_back(std::move(candidate));
                        return static_cast<int>(local_candidates.size()) >= 8;
                    }
                    const int option_idx = order[depth];
                    std::vector<Placement> placed = fixed;
                    placed.insert(placed.end(), chosen.begin(), chosen.end());
                    for (const Placement& option : options_by_block[option_idx]) {
                        if (!access_time_feasible_against_all(problem, option, placed)) {
                            continue;
                        }
                        chosen.push_back(option);
                        if (dfs(depth + 1)) {
                            return true;
                        }
                        chosen.pop_back();
                    }
                    return false;
                };

                dfs(0);
                if (local_candidates.empty()) {
                    if (debug_small_tail) {
                        std::cerr << "  dfs_fail nodes=" << nodes << "\n";
                    }
                    continue;
                }
                std::sort(local_candidates.begin(), local_candidates.end(), [&](const auto& a, const auto& b) {
                    return obj1_rank_less(problem, a, b);
                });
                int emitted_local = 0;
                for (std::vector<Placement>& local_candidate : local_candidates) {
                    const double obj1 = solution_obj1(problem, local_candidate);
                    const double objective = solution_objective(problem, local_candidate);
                    if (debug_small_tail) {
                        std::cerr << "  dfs_candidate obj1=" << obj1 << " objective=" << objective
                                  << " nodes=" << nodes << "\n";
                    }
                    if (obj1 + 1e-6 < seed_obj1 ||
                        (std::abs(obj1 - seed_obj1) < 1e-6 && objective + 1e-6 < seed_objective)) {
                        forced_outputs.push_back(local_candidate);
                        add_to_pool(problem, pool, std::move(local_candidate), max_size);
                        ++emitted_local;
                        if (static_cast<int>(forced_outputs.size()) >= 320) {
                            return;
                        }
                        if (emitted_local >= 4) {
                            break;
                        }
                    }
                }
            }
        }
    }
}

void high_w1_local_crossbay_subset_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    std::vector<std::vector<Placement>>& forced_outputs
) {
    if (pool.empty() || problem.w1 < 1000.0 || problem.blocks.size() > 140 || timer.expired(900)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        const double a_obj1 = solution_obj1(problem, a);
        const double b_obj1 = solution_obj1(problem, b);
        if (std::abs(a_obj1 - b_obj1) <= 3.0) {
            const double a_obj2 = solution_obj2_raw(problem, a);
            const double b_obj2 = solution_obj2_raw(problem, b);
            if (std::abs(a_obj2 - b_obj2) > 1e-6) {
                return a_obj2 < b_obj2;
            }
        }
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 8);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    const std::vector<int> prefix_sizes{4, 6, 8, 10, 14, 18, 24, 32, 44, 60};
    for (const std::vector<Placement>& seed : seeds) {
        if (timer.expired(900)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        if (seed_obj1 <= 0.0 || seed_obj1 > 1000.0) {
            continue;
        }
        const double seed_objective = solution_objective(problem, seed);
        const std::vector<int> targets = obj1_repair_targets(problem, seed, 12);
        for (int target_id : targets) {
            if (timer.expired(900)) {
                break;
            }
            std::vector<int> ranked{target_id};
            for (int bay_id = 0; bay_id < static_cast<int>(problem.bays.size()); ++bay_id) {
                ranked = fill_unique_blocks(
                    ranked,
                    bay_blocks_near_tardy(problem, seed, bay_id, target_id, 36),
                    72
                );
            }
            ranked = fill_unique_blocks(ranked, entry_blockers_for_target(problem, seed, target_id, 16), 88);
            ranked = fill_unique_blocks(ranked, time_window_blockers_for_target(problem, seed, target_id, 18), 104);
            ranked = fill_unique_blocks(ranked, pair_partners(problem, seed, target_id, 10), 112);

            for (int prefix_size : prefix_sizes) {
                if (timer.expired(900) || static_cast<int>(ranked.size()) < 2) {
                    break;
                }
                std::vector<int> subset(
                    ranked.begin(),
                    ranked.begin() + std::min<int>(prefix_size, static_cast<int>(ranked.size()))
                );
                if (std::find(subset.begin(), subset.end(), target_id) == subset.end()) {
                    subset.insert(subset.begin(), target_id);
                }
                std::sort(subset.begin(), subset.end());
                subset.erase(std::unique(subset.begin(), subset.end()), subset.end());

                std::vector<std::vector<int>> sequences;
                sequences.push_back(due_order_sequence(problem, subset));
                sequences.push_back(release_due_sequence(problem, subset));
                sequences.push_back(current_entry_sequence(problem, seed, subset));
                sequences.push_back(tardy_first_sequence(problem, seed, subset));
                std::vector<int> reverse_current = current_entry_sequence(problem, seed, subset);
                std::reverse(reverse_current.begin(), reverse_current.end());
                sequences.push_back(std::move(reverse_current));

                for (const std::vector<int>& sequence : sequences) {
                    if (timer.expired(900)) {
                        break;
                    }
                    for (int bay_mode = 0; bay_mode < 4 && !timer.expired(900); ++bay_mode) {
                        for (int position_mode = 0; position_mode < 4 && !timer.expired(900); ++position_mode) {
                            for (int repair_mode = 0; repair_mode < 2 && !timer.expired(900); ++repair_mode) {
                                std::vector<Placement> candidate = remove_blocks(seed, sequence);
                                const bool repaired = repair_mode == 0
                                    ? repair_sequence_obj1(problem, candidate, sequence, timer, bay_mode, position_mode)
                                    : repair_sequence(problem, candidate, sequence, timer, bay_mode, position_mode);
                                if (!repaired) {
                                    continue;
                                }
                                left_shift_obj1_pass(problem, candidate, timer);
                                relaxed_left_shift_obj1_pass(problem, candidate, timer);
                                obj1_reschedule_pass(problem, candidate, timer);
                                const double obj1 = solution_obj1(problem, candidate);
                                const double objective = solution_objective(problem, candidate);
                                if (obj1 + 1e-6 > seed_obj1 ||
                                    (std::abs(obj1 - seed_obj1) < 1e-6 &&
                                     objective >= seed_objective - 1e-6)) {
                                    continue;
                                }
                                std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                                    return a.block_id < b.block_id;
                                });
                                forced_outputs.push_back(candidate);
                                add_to_pool(problem, pool, std::move(candidate), max_size);
                                if (static_cast<int>(forced_outputs.size()) >= 240) {
                                    return;
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

void high_w1_pair_blocker_repair_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    std::vector<std::vector<Placement>>& forced_outputs
) {
    if (pool.empty() || problem.w1 < 1000.0 || problem.blocks.size() > 140 || timer.expired(900)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        const double a_obj1 = solution_obj1(problem, a);
        const double b_obj1 = solution_obj1(problem, b);
        if (std::abs(a_obj1 - b_obj1) <= 3.0) {
            const double a_obj2 = solution_obj2_raw(problem, a);
            const double b_obj2 = solution_obj2_raw(problem, b);
            if (std::abs(a_obj2 - b_obj2) > 1e-6) {
                return a_obj2 < b_obj2;
            }
        }
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 8);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    for (const std::vector<Placement>& seed : seeds) {
        if (timer.expired(900)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        if (seed_obj1 <= 0.0 || seed_obj1 > 60.0) {
            continue;
        }
        const double seed_objective = solution_objective(problem, seed);
        const std::vector<int> targets = due_order_sequence(problem, obj1_repair_targets(problem, seed, 8));
        for (int target_id : targets) {
            if (timer.expired(900)) {
                break;
            }
            const Placement current_target = find_placement(seed, target_id);
            const Block& target_block = problem.blocks[target_id];
            const int target_tardy = std::max(0, current_target.exit - target_block.due);
            if (target_tardy <= 0 || target_tardy > 8) {
                continue;
            }
            const int ideal_entry = std::max(target_block.release, target_block.due - target_block.processing);
            if (ideal_entry >= current_target.entry) {
                continue;
            }
            Placement ideal_target = current_target;
            ideal_target.entry = ideal_entry;
            ideal_target.exit = ideal_entry + target_block.processing;

            std::vector<Placement> without_target = remove_block(seed, target_id);
            std::vector<int> blockers = placement_access_blockers(problem, ideal_target, without_target, 6);
            std::vector<std::pair<std::tuple<int, int, int>, int>> overlapping;
            for (const Placement& other : without_target) {
                if (other.bay_id != ideal_target.bay_id ||
                    !(ideal_target.entry < other.exit && other.entry < ideal_target.exit)) {
                    continue;
                }
                const Block& other_block = problem.blocks[other.block_id];
                overlapping.push_back(
                    {
                        std::make_tuple(other.exit, other_block.due, other.block_id),
                        other.block_id,
                    }
                );
            }
            std::sort(overlapping.begin(), overlapping.end());
            for (const auto& item : overlapping) {
                if (std::find(blockers.begin(), blockers.end(), item.second) == blockers.end()) {
                    blockers.push_back(item.second);
                    if (static_cast<int>(blockers.size()) >= 8) {
                        break;
                    }
                }
            }
            if (blockers.empty()) {
                continue;
            }
            for (int blocker_id : blockers) {
                if (timer.expired(900)) {
                    break;
                }
                const Placement current_blocker = find_placement(seed, blocker_id);
                const Block& blocker_block = problem.blocks[blocker_id];
                std::vector<Placement> base = remove_blocks(seed, std::vector<int>{target_id, blocker_id});
                if (!access_time_feasible_against_all(problem, ideal_target, base) &&
                    !relaxed_time_feasible_against_all(problem, ideal_target, base)) {
                    continue;
                }
                base.push_back(ideal_target);

                std::vector<Placement> best_candidate;
                double best_obj1 = seed_obj1;
                double best_objective = seed_objective;
                const int blocker_orient_count = static_cast<int>(blocker_block.boxes.size());
                const int x_slot_count = 4;
                const int y_slot_count = 3;
                const int slot_count =
                    static_cast<int>(problem.bays.size()) * blocker_orient_count * x_slot_count * y_slot_count;
                std::vector<std::vector<Placement>> slot_candidates(slot_count);
                std::vector<double> slot_obj1(slot_count, seed_obj1);
                std::vector<double> slot_objective(slot_count, seed_objective);
                const int latest_on_time_entry = blocker_block.due - blocker_block.processing;
                const int latest_trial_entry =
                    std::max({blocker_block.release, latest_on_time_entry + target_tardy, current_blocker.entry + target_tardy});
                for (int bay_id = 0; bay_id < static_cast<int>(problem.bays.size()) && !timer.expired(900); ++bay_id) {
                    for (int orient_idx = 0; orient_idx < static_cast<int>(blocker_block.boxes.size()) &&
                                             !timer.expired(900);
                         ++orient_idx) {
                        std::vector<std::pair<int, int>> positions =
                            candidate_positions(problem, bay_id, blocker_block.boxes[orient_idx], base, 3);
                        if (bay_id == current_blocker.bay_id && orient_idx == current_blocker.orient_idx &&
                            fits(problem.bays[bay_id], blocker_block.boxes[orient_idx], current_blocker.x, current_blocker.y)) {
                            positions.insert(positions.begin(), {current_blocker.x, current_blocker.y});
                        }
                        const BBox& blocker_box = blocker_block.boxes[orient_idx];
                        const int min_x = lower_x(blocker_box);
                        const int max_x = std::max(
                            min_x,
                            static_cast<int>(std::floor(problem.bays[bay_id].width - blocker_box.max_x + 1e-9))
                        );
                        const int min_y = lower_y(blocker_box);
                        const int max_y = std::max(
                            min_y,
                            static_cast<int>(std::floor(problem.bays[bay_id].height - blocker_box.max_y + 1e-9))
                        );
                        for (int gx = min_x; gx <= max_x; ++gx) {
                            for (int gy = min_y; gy <= max_y; ++gy) {
                                if (fits(problem.bays[bay_id], blocker_box, gx, gy) &&
                                    std::find(positions.begin(), positions.end(), std::make_pair(gx, gy)) == positions.end()) {
                                    positions.push_back({gx, gy});
                                }
                            }
                        }
                        int checked_positions = 0;
                        for (const auto& [x, y] : positions) {
                            if (timer.expired(900) || ++checked_positions > 12000) {
                                break;
                            }
                            for (int start = blocker_block.release;
                                 start <= latest_trial_entry && !timer.expired(900);
                                 ++start) {
                                Placement blocker;
                                blocker.block_id = blocker_id;
                                blocker.bay_id = bay_id;
                                blocker.orient_idx = orient_idx;
                                blocker.x = x;
                                blocker.y = y;
                                blocker.entry = start;
                                blocker.exit = start + blocker_block.processing;
                                blocker.workload = blocker_block.workload;
                                const bool blocker_access_ok = access_time_feasible_against_all(problem, blocker, base);
                                const bool blocker_relaxed_ok = relaxed_time_feasible_against_all(problem, blocker, base);
                                if (!blocker_access_ok && !blocker_relaxed_ok) {
                                    continue;
                                }
                                std::vector<Placement> candidate = base;
                                candidate.push_back(blocker);
                                std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                                    return a.block_id < b.block_id;
                                });
                                const double obj1 = solution_obj1(problem, candidate);
                                const double objective = solution_objective(problem, candidate);
                                const int x_bin = std::min(
                                    x_slot_count - 1,
                                    std::max(0, (x - min_x) * x_slot_count / std::max(1, max_x - min_x + 1))
                                );
                                const int y_bin = std::min(
                                    y_slot_count - 1,
                                    std::max(0, (y - min_y) * y_slot_count / std::max(1, max_y - min_y + 1))
                                );
                                const int slot =
                                    ((bay_id * blocker_orient_count + orient_idx) * x_slot_count + x_bin) *
                                        y_slot_count +
                                    y_bin;
                                if (slot >= 0 && slot < slot_count && obj1 + 1e-6 < seed_obj1 &&
                                    (slot_candidates[slot].empty() || obj1 + 1e-6 < slot_obj1[slot] ||
                                     (std::abs(obj1 - slot_obj1[slot]) < 1e-6 &&
                                      objective + 1e-6 < slot_objective[slot]))) {
                                    slot_candidates[slot] = candidate;
                                    slot_obj1[slot] = obj1;
                                    slot_objective[slot] = objective;
                                }
                                if (obj1 + 1e-6 < best_obj1 ||
                                    (std::abs(obj1 - best_obj1) < 1e-6 &&
                                     objective + 1e-6 < best_objective)) {
                                    best_candidate = std::move(candidate);
                                    best_obj1 = obj1;
                                    best_objective = objective;
                                }
                            }
                        }
                    }
                }
                for (std::vector<Placement>& slot_candidate : slot_candidates) {
                    if (slot_candidate.empty()) {
                        continue;
                    }
                    left_shift_obj1_pass(problem, slot_candidate, timer);
                    relaxed_left_shift_obj1_pass(problem, slot_candidate, timer);
                    obj1_reschedule_pass(problem, slot_candidate, timer);
                    if (solution_obj1(problem, slot_candidate) + 1e-6 < seed_obj1) {
                        forced_outputs.push_back(slot_candidate);
                        add_to_pool(problem, pool, slot_candidate, max_size);
                        if (static_cast<int>(forced_outputs.size()) >= 260) {
                            return;
                        }
                    }
                }
                if (!best_candidate.empty() && best_obj1 + 1e-6 < seed_obj1) {
                    left_shift_obj1_pass(problem, best_candidate, timer);
                    relaxed_left_shift_obj1_pass(problem, best_candidate, timer);
                    obj1_reschedule_pass(problem, best_candidate, timer);
                    forced_outputs.push_back(best_candidate);
                    add_to_pool(problem, pool, std::move(best_candidate), max_size);
                    if (static_cast<int>(forced_outputs.size()) >= 260) {
                        return;
                    }
                }
            }
        }
    }
}

void high_w1_small_tail_blocker_push_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    std::vector<std::vector<Placement>>& forced_outputs
) {
    if (pool.empty() || problem.w1 < 1000.0 || problem.blocks.size() > 140 || timer.expired(900)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 6);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    for (const std::vector<Placement>& seed : seeds) {
        if (timer.expired(900)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        const bool medium_three_bay_tail =
            problem.bays.size() >= 3 && problem.w3 <= 200.0 && seed_obj1 <= 180.0;
        const double blocker_push_obj1_limit = medium_three_bay_tail ? 180.0 : 60.0;
        const int max_target_tardy = medium_three_bay_tail ? 20 : 8;
        const int max_repair_targets = medium_three_bay_tail ? 28 : 16;
        const int max_blockers = medium_three_bay_tail ? 8 : 4;
        if (seed_obj1 <= 0.0 || seed_obj1 > blocker_push_obj1_limit) {
            continue;
        }
        const double seed_objective = solution_objective(problem, seed);
        const std::vector<int> targets = obj1_repair_targets(problem, seed, max_repair_targets);
        for (int target_id : targets) {
            if (timer.expired(900)) {
                break;
            }
            const Placement current_target = find_placement(seed, target_id);
            const Block& target_block = problem.blocks[target_id];
            const int target_tardy = std::max(0, current_target.exit - target_block.due);
            if (target_tardy <= 0 || target_tardy > max_target_tardy) {
                continue;
            }
            const int ideal_entry = std::max(target_block.release, target_block.due - target_block.processing);
            if (ideal_entry >= current_target.entry) {
                continue;
            }

            Placement ideal_target = current_target;
            ideal_target.entry = ideal_entry;
            ideal_target.exit = ideal_entry + target_block.processing;
            std::vector<Placement> without_target = remove_block(seed, target_id);
            std::vector<int> blockers = placement_access_blockers(problem, ideal_target, without_target, max_blockers);
            if (blockers.empty()) {
                blockers = time_window_blockers_for_target(problem, seed, target_id, max_blockers);
            }

            for (int blocker_id : blockers) {
                if (timer.expired(900)) {
                    break;
                }
                if (blocker_id == target_id) {
                    continue;
                }
                const Placement current_blocker = find_placement(seed, blocker_id);
                const Block& blocker_block = problem.blocks[blocker_id];
                std::vector<Placement> base = remove_blocks(seed, std::vector<int>{target_id, blocker_id});
                if (!access_time_feasible_against_all(problem, ideal_target, base)) {
                    continue;
                }
                base.push_back(ideal_target);

                std::vector<std::pair<int, int>> bay_order;
                bay_order.reserve(problem.bays.size());
                bay_order.push_back({current_blocker.bay_id == ideal_target.bay_id ? 1 : 0, current_blocker.bay_id});
                for (int bay_id = 0; bay_id < static_cast<int>(problem.bays.size()); ++bay_id) {
                    if (bay_id != current_blocker.bay_id) {
                        bay_order.push_back({bay_id == ideal_target.bay_id ? 1 : 0, bay_id});
                    }
                }
                std::sort(bay_order.begin(), bay_order.end());

                std::vector<Placement> best_candidate;
                double best_obj1 = seed_obj1;
                double best_objective = seed_objective;
                for (const auto& bay_item : bay_order) {
                    const int bay_id = bay_item.second;
                    if (timer.expired(900)) {
                        break;
                    }
                    for (int orient_idx = 0; orient_idx < static_cast<int>(blocker_block.boxes.size()) &&
                                             !timer.expired(900);
                         ++orient_idx) {
                        std::vector<std::pair<int, int>> positions =
                            candidate_positions(problem, bay_id, blocker_block.boxes[orient_idx], base, 3);
                        if (bay_id == current_blocker.bay_id && orient_idx == current_blocker.orient_idx &&
                            fits(problem.bays[bay_id], blocker_block.boxes[orient_idx], current_blocker.x, current_blocker.y)) {
                            positions.insert(positions.begin(), {current_blocker.x, current_blocker.y});
                        }
                        const BBox& box = blocker_block.boxes[orient_idx];
                        const int min_x = lower_x(box);
                        const int max_x = std::max(
                            min_x,
                            static_cast<int>(std::floor(problem.bays[bay_id].width - box.max_x + 1e-9))
                        );
                        const int min_y = lower_y(box);
                        const int max_y = std::max(
                            min_y,
                            static_cast<int>(std::floor(problem.bays[bay_id].height - box.max_y + 1e-9))
                        );
                        for (int dx : {-6, -3, 0, 3, 6}) {
                            for (int dy : {-6, -3, 0, 3, 6}) {
                                const int x = std::max(min_x, std::min(max_x, current_blocker.x + dx));
                                const int y = std::max(min_y, std::min(max_y, current_blocker.y + dy));
                                positions.push_back({x, y});
                            }
                        }
                        positions.push_back({min_x, min_y});
                        positions.push_back({min_x, max_y});
                        positions.push_back({max_x, min_y});
                        positions.push_back({max_x, max_y});
                        std::sort(positions.begin(), positions.end());
                        positions.erase(std::unique(positions.begin(), positions.end()), positions.end());

                        std::vector<int> starts;
                        auto add_start = [&](int start) {
                            if (start < blocker_block.release) {
                                return;
                            }
                            if (std::find(starts.begin(), starts.end(), start) == starts.end()) {
                                starts.push_back(start);
                            }
                        };
                        add_start(current_blocker.entry);
                        add_start(current_blocker.entry + target_tardy);
                        add_start(ideal_target.entry + 1);
                        add_start(ideal_target.exit);
                        add_start(std::max(blocker_block.release, blocker_block.due - blocker_block.processing));
                        for (int delta = 1; delta <= 16; ++delta) {
                            add_start(current_blocker.entry + delta);
                        }
                        std::sort(starts.begin(), starts.end(), [&](int a, int b) {
                            const int da = std::abs(a - current_blocker.entry);
                            const int db = std::abs(b - current_blocker.entry);
                            if (da != db) {
                                return da < db;
                            }
                            return a < b;
                        });

                        int checked_positions = 0;
                        for (const auto& [x, y] : positions) {
                            if (timer.expired(900) || ++checked_positions > 1800) {
                                break;
                            }
                            for (int start : starts) {
                                if (timer.expired(900)) {
                                    break;
                                }
                                Placement blocker;
                                blocker.block_id = blocker_id;
                                blocker.bay_id = bay_id;
                                blocker.orient_idx = orient_idx;
                                blocker.x = x;
                                blocker.y = y;
                                blocker.entry = start;
                                blocker.exit = start + blocker_block.processing;
                                blocker.workload = blocker_block.workload;
                                if (!access_time_feasible_against_all(problem, blocker, base)) {
                                    continue;
                                }
                                std::vector<Placement> candidate = base;
                                candidate.push_back(blocker);
                                std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                                    return a.block_id < b.block_id;
                                });
                                if (!solution_feasible_like(problem, candidate)) {
                                    continue;
                                }
                                const double obj1 = solution_obj1(problem, candidate);
                                const double objective = solution_objective(problem, candidate);
                                if (obj1 + 1e-6 < best_obj1 ||
                                    (std::abs(obj1 - best_obj1) < 1e-6 && objective + 1e-6 < best_objective)) {
                                    best_candidate = std::move(candidate);
                                    best_obj1 = obj1;
                                    best_objective = objective;
                                }
                            }
                        }
                    }
                }
                if (!best_candidate.empty() && best_obj1 + 1e-6 < seed_obj1) {
                    left_shift_obj1_pass(problem, best_candidate, timer);
                    relaxed_left_shift_obj1_pass(problem, best_candidate, timer);
                    obj1_reschedule_pass(problem, best_candidate, timer);
                    if (solution_obj1(problem, best_candidate) + 1e-6 < seed_obj1) {
                        forced_outputs.push_back(best_candidate);
                        add_to_pool(problem, pool, std::move(best_candidate), max_size);
                        if (static_cast<int>(forced_outputs.size()) >= 360) {
                            return;
                        }
                    }
                }
            }
        }
    }
}

void high_w1_single_grid_ontime_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    std::vector<std::vector<Placement>>& forced_outputs
) {
    if (pool.empty() || problem.w1 < 1000.0 || problem.blocks.size() > 140 || timer.expired(900)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 4);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    for (const std::vector<Placement>& seed : seeds) {
        if (timer.expired(900)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        if (seed_obj1 <= 250.0 || seed_obj1 > 1000.0) {
            continue;
        }
        const double seed_objective = solution_objective(problem, seed);
        const std::vector<int> targets = obj1_repair_targets(problem, seed, 16);
        for (int target_id : targets) {
            if (timer.expired(900)) {
                break;
            }
            const Placement current = find_placement(seed, target_id);
            const Block& block = problem.blocks[target_id];
            const int current_tardy = std::max(0, current.exit - block.due);
            if (current_tardy <= 0) {
                continue;
            }
            const int desired_entry = block.due - block.processing;
            if (desired_entry < block.release) {
                continue;
            }
            std::vector<Placement> base = remove_block(seed, target_id);
            std::vector<std::vector<Placement>> slot_candidates;
            std::vector<double> slot_obj1;
            std::vector<double> slot_objective;
            const int orient_count = static_cast<int>(block.boxes.size());
            const int x_slot_count = 20;
            const int y_slot_count = 8;
            const int slot_count = static_cast<int>(problem.bays.size()) * orient_count * x_slot_count * y_slot_count;
            slot_candidates.resize(slot_count);
            slot_obj1.assign(slot_count, seed_obj1);
            slot_objective.assign(slot_count, seed_objective);

            for (int bay_id = 0; bay_id < static_cast<int>(problem.bays.size()) && !timer.expired(900); ++bay_id) {
                for (int orient_idx = 0; orient_idx < orient_count && !timer.expired(900); ++orient_idx) {
                    const BBox& box = block.boxes[orient_idx];
                    const int min_x = lower_x(box);
                    const int max_x = std::max(
                        min_x,
                        static_cast<int>(std::floor(problem.bays[bay_id].width - box.max_x + 1e-9))
                    );
                    const int min_y = lower_y(box);
                    const int max_y = std::max(
                        min_y,
                        static_cast<int>(std::floor(problem.bays[bay_id].height - box.max_y + 1e-9))
                    );
                    int checked_positions = 0;
                    for (int x = min_x; x <= max_x && !timer.expired(900); ++x) {
                        for (int y = min_y; y <= max_y && !timer.expired(900); ++y) {
                            if (++checked_positions > 22000) {
                                break;
                            }
                            if (!fits(problem.bays[bay_id], box, x, y)) {
                                continue;
                            }
                            Placement placement;
                            placement.block_id = target_id;
                            placement.bay_id = bay_id;
                            placement.orient_idx = orient_idx;
                            placement.x = x;
                            placement.y = y;
                            placement.entry = desired_entry;
                            placement.exit = desired_entry + block.processing;
                            placement.workload = block.workload;
                            std::vector<Placement> candidate = base;
                            candidate.push_back(placement);
                            std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                                return a.block_id < b.block_id;
                            });
                            const double obj1 = solution_obj1(problem, candidate);
                            if (obj1 + 1e-6 >= seed_obj1) {
                                continue;
                            }
                            const double objective = solution_objective(problem, candidate);
                            const int x_bin = std::min(
                                x_slot_count - 1,
                                std::max(0, (x - min_x) * x_slot_count / std::max(1, max_x - min_x + 1))
                            );
                            const int y_bin = std::min(
                                y_slot_count - 1,
                                std::max(0, (y - min_y) * y_slot_count / std::max(1, max_y - min_y + 1))
                            );
                            const int slot =
                                ((bay_id * orient_count + orient_idx) * x_slot_count + x_bin) * y_slot_count + y_bin;
                            const int placement_distance =
                                std::abs(placement.x - current.x) + std::abs(placement.y - current.y) +
                                8 * (placement.bay_id != current.bay_id) + 2 * (placement.orient_idx != current.orient_idx);
                            if (slot < 0 || slot >= slot_count) {
                                continue;
                            }
                            const Placement previous =
                                slot_candidates[slot].empty() ? current : find_placement(slot_candidates[slot], target_id);
                            const int previous_distance =
                                std::abs(previous.x - current.x) + std::abs(previous.y - current.y) +
                                8 * (previous.bay_id != current.bay_id) + 2 * (previous.orient_idx != current.orient_idx);
                            if (slot_candidates[slot].empty() || obj1 + 1e-6 < slot_obj1[slot] ||
                                (std::abs(obj1 - slot_obj1[slot]) < 1e-6 &&
                                 (objective + 1e-6 < slot_objective[slot] ||
                                  (std::abs(objective - slot_objective[slot]) < 1e-6 &&
                                   placement_distance < previous_distance)))) {
                                slot_candidates[slot] = std::move(candidate);
                                slot_obj1[slot] = obj1;
                                slot_objective[slot] = objective;
                            }
                        }
                    }
                }
            }
            for (std::vector<Placement>& candidate : slot_candidates) {
                if (candidate.empty()) {
                    continue;
                }
                forced_outputs.push_back(candidate);
                if (static_cast<int>(forced_outputs.size()) >= 360) {
                    return;
                }
            }
        }
    }
}

void high_w1_all_tardy_crossbay_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    std::vector<std::vector<Placement>>& forced_outputs
) {
    if (pool.empty() || problem.w1 < 1000.0 || problem.blocks.size() > 140 || timer.expired(900)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 8);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    const std::vector<int> prefix_sizes{12, 18, 24, 32, 44, 60, 78, 96};
    for (const std::vector<Placement>& seed : seeds) {
        if (timer.expired(900)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        if (seed_obj1 <= 0.0 || seed_obj1 > 250.0) {
            continue;
        }
        const double seed_objective = solution_objective(problem, seed);
        const std::vector<int> tardy_targets = obj1_repair_targets(problem, seed, 24);
        if (tardy_targets.size() < 2) {
            continue;
        }

        std::vector<int> ranked = tardy_targets;
        for (int target_id : tardy_targets) {
            for (int bay_id = 0; bay_id < static_cast<int>(problem.bays.size()); ++bay_id) {
                ranked = fill_unique_blocks(
                    ranked,
                    bay_blocks_near_tardy(problem, seed, bay_id, target_id, 42),
                    120
                );
            }
            ranked = fill_unique_blocks(ranked, entry_blockers_for_target(problem, seed, target_id, 20), 132);
            ranked = fill_unique_blocks(ranked, time_window_blockers_for_target(problem, seed, target_id, 24), 144);
            ranked = fill_unique_blocks(ranked, pair_partners(problem, seed, target_id, 12), 156);
        }

        for (int prefix_size : prefix_sizes) {
            if (timer.expired(900) || ranked.empty()) {
                break;
            }
            std::vector<int> subset(
                ranked.begin(),
                ranked.begin() + std::min<int>(prefix_size, static_cast<int>(ranked.size()))
            );
            subset = fill_unique_blocks(subset, tardy_targets, prefix_size + static_cast<int>(tardy_targets.size()));
            std::sort(subset.begin(), subset.end());
            subset.erase(std::unique(subset.begin(), subset.end()), subset.end());

            std::vector<int> tardy_due = tardy_first_sequence(problem, seed, tardy_targets);
            std::vector<int> rest = subset;
            for (int tardy_id : tardy_targets) {
                rest.erase(std::remove(rest.begin(), rest.end(), tardy_id), rest.end());
            }
            std::vector<int> rest_due = due_order_sequence(problem, rest);
            std::vector<int> rest_release = release_due_sequence(problem, rest);
            std::vector<int> tardy_then_due = tardy_due;
            tardy_then_due.insert(tardy_then_due.end(), rest_due.begin(), rest_due.end());
            std::vector<int> tardy_then_release = tardy_due;
            tardy_then_release.insert(tardy_then_release.end(), rest_release.begin(), rest_release.end());

            std::vector<std::vector<int>> sequences;
            sequences.push_back(due_order_sequence(problem, subset));
            sequences.push_back(release_due_sequence(problem, subset));
            sequences.push_back(current_entry_sequence(problem, seed, subset));
            sequences.push_back(tardy_first_sequence(problem, seed, subset));
            sequences.push_back(std::move(tardy_then_due));
            sequences.push_back(std::move(tardy_then_release));
            std::vector<int> reverse_current = current_entry_sequence(problem, seed, subset);
            std::reverse(reverse_current.begin(), reverse_current.end());
            sequences.push_back(std::move(reverse_current));

            for (const std::vector<int>& sequence : sequences) {
                if (timer.expired(900)) {
                    break;
                }
                for (int bay_mode = 0; bay_mode < 4 && !timer.expired(900); ++bay_mode) {
                    for (int position_mode = 0; position_mode < 4 && !timer.expired(900); ++position_mode) {
                        for (int repair_mode = 0; repair_mode < 2 && !timer.expired(900); ++repair_mode) {
                            std::vector<Placement> candidate = remove_blocks(seed, sequence);
                            const bool repaired = repair_mode == 0
                                ? repair_sequence_obj1(problem, candidate, sequence, timer, bay_mode, position_mode)
                                : repair_sequence(problem, candidate, sequence, timer, bay_mode, position_mode);
                            if (!repaired) {
                                continue;
                            }
                            left_shift_obj1_pass(problem, candidate, timer);
                            relaxed_left_shift_obj1_pass(problem, candidate, timer);
                            obj1_reschedule_pass(problem, candidate, timer);
                            const double obj1 = solution_obj1(problem, candidate);
                            const double objective = solution_objective(problem, candidate);
                            if (obj1 + 1e-6 > seed_obj1 ||
                                (std::abs(obj1 - seed_obj1) < 1e-6 &&
                                 objective >= seed_objective - 1e-6)) {
                                continue;
                            }
                            std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                                return a.block_id < b.block_id;
                            });
                            forced_outputs.push_back(candidate);
                            add_to_pool(problem, pool, std::move(candidate), max_size);
                            if (static_cast<int>(forced_outputs.size()) >= 320) {
                                return;
                            }
                        }
                    }
                }
            }
        }
    }
}

void high_w1_random_tardy_cluster_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    std::vector<std::vector<Placement>>& forced_outputs
) {
    if (pool.empty() || problem.w1 < 1000.0 || problem.blocks.size() > 140 || timer.expired(900)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 6);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    int seed_index = 0;
    for (const std::vector<Placement>& seed : seeds) {
        if (timer.expired(900)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        if (seed_obj1 <= 0.0 || seed_obj1 > 250.0) {
            ++seed_index;
            continue;
        }
        const double seed_objective = solution_objective(problem, seed);
        const std::vector<int> tardy_targets = obj1_repair_targets(problem, seed, 24);
        if (tardy_targets.empty()) {
            ++seed_index;
            continue;
        }
        std::vector<int> cluster = tardy_targets;
        for (int target_id : tardy_targets) {
            for (int bay_id = 0; bay_id < static_cast<int>(problem.bays.size()); ++bay_id) {
                cluster = fill_unique_blocks(
                    cluster,
                    bay_blocks_near_tardy(problem, seed, bay_id, target_id, 48),
                    132
                );
            }
            cluster = fill_unique_blocks(cluster, entry_blockers_for_target(problem, seed, target_id, 24), 148);
            cluster = fill_unique_blocks(cluster, time_window_blockers_for_target(problem, seed, target_id, 28), 164);
            cluster = fill_unique_blocks(cluster, pair_partners(problem, seed, target_id, 12), 176);
        }

        std::vector<int> fixed_tardy = tardy_first_sequence(problem, seed, tardy_targets);
        std::vector<int> rest = cluster;
        for (int tardy_id : tardy_targets) {
            rest.erase(std::remove(rest.begin(), rest.end(), tardy_id), rest.end());
        }
        if (rest.empty()) {
            ++seed_index;
            continue;
        }
        const int attempt_limit = seed_obj1 <= 10.0 ? 220 : 120;
        Rng rng(1469598103934665603ULL ^ static_cast<uint64_t>(problem.blocks.size() * 131 + seed_index * 977));
        for (int attempt = 0; attempt < attempt_limit && !timer.expired(900); ++attempt) {
            std::vector<int> shuffled = rest;
            for (int i = static_cast<int>(shuffled.size()) - 1; i > 0; --i) {
                const int j = rng.next_int(i + 1);
                std::swap(shuffled[i], shuffled[j]);
            }

            std::vector<int> sequence;
            if (attempt % 4 == 0) {
                sequence = fixed_tardy;
                sequence.insert(sequence.end(), shuffled.begin(), shuffled.end());
            } else if (attempt % 4 == 1) {
                sequence = shuffled;
                sequence.insert(sequence.end(), fixed_tardy.begin(), fixed_tardy.end());
            } else if (attempt % 4 == 2) {
                sequence = due_order_sequence(problem, cluster);
                const int swap_count = std::min<int>(8, static_cast<int>(sequence.size()));
                for (int k = 0; k < swap_count; ++k) {
                    const int a = rng.next_int(static_cast<int>(sequence.size()));
                    const int b = rng.next_int(static_cast<int>(sequence.size()));
                    std::swap(sequence[a], sequence[b]);
                }
            } else {
                sequence = release_due_sequence(problem, cluster);
                const int swap_count = std::min<int>(8, static_cast<int>(sequence.size()));
                for (int k = 0; k < swap_count; ++k) {
                    const int a = rng.next_int(static_cast<int>(sequence.size()));
                    const int b = rng.next_int(static_cast<int>(sequence.size()));
                    std::swap(sequence[a], sequence[b]);
                }
            }
            {
                std::vector<int> deduped;
                deduped.reserve(sequence.size());
                std::set<int> seen;
                for (int block_id : sequence) {
                    if (seen.insert(block_id).second) {
                        deduped.push_back(block_id);
                    }
                }
                sequence.swap(deduped);
            }

            for (int bay_mode = 0; bay_mode < 4 && !timer.expired(900); ++bay_mode) {
                for (int position_mode = 0; position_mode < 4 && !timer.expired(900); ++position_mode) {
                    std::vector<Placement> candidate = remove_blocks(seed, sequence);
                    if (!repair_sequence_obj1(problem, candidate, sequence, timer, bay_mode, position_mode)) {
                        continue;
                    }
                    left_shift_obj1_pass(problem, candidate, timer);
                    relaxed_left_shift_obj1_pass(problem, candidate, timer);
                    obj1_reschedule_pass(problem, candidate, timer);
                    const double obj1 = solution_obj1(problem, candidate);
                    const double objective = solution_objective(problem, candidate);
                    if (obj1 + 1e-6 > seed_obj1 ||
                        (std::abs(obj1 - seed_obj1) < 1e-6 && objective >= seed_objective - 1e-6)) {
                        continue;
                    }
                    std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                        return a.block_id < b.block_id;
                    });
                    forced_outputs.push_back(candidate);
                    add_to_pool(problem, pool, std::move(candidate), max_size);
                    if (static_cast<int>(forced_outputs.size()) >= 420) {
                        return;
                    }
                }
            }
        }
        ++seed_index;
    }
}

void due_window_seed_relocate_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    std::vector<std::vector<Placement>>* forced_outputs = nullptr
) {
    if (pool.empty() || !problem_has_polygon_payload(problem) || timer.expired(80)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 8);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    for (const std::vector<Placement>& seed : seeds) {
        if (timer.expired(80)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        if (seed_obj1 <= 0.0 || seed_obj1 > 10000.0) {
            continue;
        }
        const double seed_objective = solution_objective(problem, seed);
        const int max_due_window_targets = seed_obj1 > 1000.0 ? 96 : 24;
        const std::vector<int> targets = obj1_repair_targets(problem, seed, max_due_window_targets);
        std::vector<std::vector<Placement>> seed_improvements;
        for (int target_id : targets) {
            if (timer.expired(80)) {
                break;
            }
            const Block& block = problem.blocks[target_id];
            const int desired_entry = block.due - block.processing;
            if (desired_entry < block.release) {
                continue;
            }
            std::vector<Placement> base = remove_block(seed, target_id);
            std::vector<int> bay_order(problem.bays.size());
            for (size_t bay_idx = 0; bay_idx < bay_order.size(); ++bay_idx) {
                bay_order[bay_idx] = static_cast<int>(bay_idx);
            }
            const Placement current = find_placement(seed, target_id);
            const int current_target_tardy = std::max(0, current.exit - block.due);
            const bool small_tail_target = seed_obj1 <= 10.0 && current_target_tardy <= 2;
            const bool high_w1_short_tail_target =
                problem.w1 >= 1000.0 &&
                problem.blocks.size() <= 140 &&
                seed_obj1 <= 200.0 &&
                current_target_tardy <= 20;
            const bool blocker_chain_target = small_tail_target || high_w1_short_tail_target;
            const int best_pref = *std::max_element(block.preferences.begin(), block.preferences.end());
            std::sort(bay_order.begin(), bay_order.end(), [&](int a, int b) {
                if (small_tail_target && (a == current.bay_id) != (b == current.bay_id)) {
                    return a == current.bay_id;
                }
                const int loss_a = best_pref - block.preferences[a];
                const int loss_b = best_pref - block.preferences[b];
                if (loss_a != loss_b) {
                    return loss_a < loss_b;
                }
                if ((a == current.bay_id) != (b == current.bay_id)) {
                    return a == current.bay_id;
                }
                return a < b;
            });

            std::vector<std::vector<Placement>> target_candidates;
            for (int bay_id : bay_order) {
                if (timer.expired(80)) {
                    break;
                }
                std::vector<Placement> best_bay;
                double best_bay_obj1 = seed_obj1;
                double best_bay_objective = seed_objective;
                for (int orient_idx = 0; orient_idx < static_cast<int>(block.boxes.size()); ++orient_idx) {
                    if (timer.expired(80)) {
                        break;
                    }
                    int checked_positions = 0;
                    std::vector<std::pair<int, int>> positions =
                        candidate_positions(problem, bay_id, block.boxes[orient_idx], base, 3);
                    if (bay_id == current.bay_id && orient_idx == current.orient_idx &&
                        fits(problem.bays[bay_id], block.boxes[orient_idx], current.x, current.y)) {
                        positions.insert(positions.begin(), {current.x, current.y});
                    }
                    for (const auto& [x, y] : positions) {
                        if (timer.expired(80) || ++checked_positions > 900) {
                            break;
                        }
                        const int target_candidate_cap =
                            small_tail_target ? 640 : (high_w1_short_tail_target ? 160 : (seed_obj1 > 1000.0 ? 24 : 6));
                        if (static_cast<int>(target_candidates.size()) >= target_candidate_cap) {
                            break;
                        }
                        Placement placement;
                        placement.block_id = target_id;
                        placement.bay_id = bay_id;
                        placement.orient_idx = orient_idx;
                        placement.x = x;
                        placement.y = y;
                        placement.entry = desired_entry;
                        placement.exit = desired_entry + block.processing;
                        placement.workload = block.workload;
                        const bool relaxed_feasible = relaxed_time_feasible_against_all(problem, placement, base);
                        const bool access_feasible =
                            blocker_chain_target ? access_time_feasible_against_all(problem, placement, base) : relaxed_feasible;
                        if (!access_feasible) {
                            const Placement current_target = find_placement(seed, target_id);
                            const int target_tardy = std::max(0, current_target.exit - block.due);
                            if (blocker_chain_target) {
                                const int max_direct_blockers = high_w1_short_tail_target ? 4 : 2;
                                std::vector<int> blockers = placement_access_blockers(problem, placement, base, max_direct_blockers);
                                if (blockers.empty() && bay_id == current_target.bay_id) {
                                    std::vector<std::pair<std::tuple<int, int, int>, int>> overlapping;
                                    for (const Placement& other : base) {
                                        if (other.bay_id != placement.bay_id ||
                                            !(placement.entry < other.exit && other.entry < placement.exit)) {
                                            continue;
                                        }
                                        const Block& other_block = problem.blocks[other.block_id];
                                        overlapping.push_back(
                                            {
                                                std::make_tuple(other.exit, other_block.due, other.block_id),
                                                other.block_id,
                                            }
                                        );
                                    }
                                    std::sort(overlapping.begin(), overlapping.end());
                                    for (const auto& item : overlapping) {
                                        blockers.push_back(item.second);
                                        if (static_cast<int>(blockers.size()) >= 3) {
                                            break;
                                        }
                                    }
                                }
                                if (bay_id == current_target.bay_id && blockers.empty()) {
                                    std::vector<Placement> proxy_blocked_candidate = base;
                                    proxy_blocked_candidate.push_back(placement);
                                    std::sort(
                                        proxy_blocked_candidate.begin(),
                                        proxy_blocked_candidate.end(),
                                        [](const Placement& a, const Placement& b) {
                                            return a.block_id < b.block_id;
                                        }
                                    );
                                    const double proxy_blocked_obj1 = solution_obj1(problem, proxy_blocked_candidate);
                                    if (proxy_blocked_obj1 + 1e-6 < seed_obj1) {
                                        const double proxy_blocked_objective =
                                            solution_objective(problem, proxy_blocked_candidate);
                                        if (best_bay.empty() || proxy_blocked_obj1 + 1e-6 < best_bay_obj1 ||
                                            (std::abs(proxy_blocked_obj1 - best_bay_obj1) < 1e-6 &&
                                             proxy_blocked_objective + 1e-6 < best_bay_objective)) {
                                            best_bay = proxy_blocked_candidate;
                                            best_bay_obj1 = proxy_blocked_obj1;
                                            best_bay_objective = proxy_blocked_objective;
                                        }
                                        if (static_cast<int>(target_candidates.size()) < target_candidate_cap) {
                                            target_candidates.push_back(std::move(proxy_blocked_candidate));
                                        }
                                    }
                                }
                                if (!blockers.empty()) {
                                    if ((seed_obj1 <= 2.0 ||
                                        (problem.w1 >= 1000.0 && seed_obj1 <= 200.0)) &&
                                        static_cast<int>(blockers.size()) >= 2) {
                                        for (int blocker_count = 2;
                                             blocker_count <= std::min<int>(static_cast<int>(blockers.size()), 3) &&
                                             !timer.expired(80);
                                             ++blocker_count) {
                                            std::vector<int> removed{target_id};
                                            std::vector<int> sequence_blockers;
                                            for (int blocker_idx = 0; blocker_idx < blocker_count; ++blocker_idx) {
                                                removed.push_back(blockers[blocker_idx]);
                                                sequence_blockers.push_back(blockers[blocker_idx]);
                                            }
                                            std::vector<Placement> repair_base = remove_blocks(seed, removed);
                                            if (!access_time_feasible_against_all(problem, placement, repair_base)) {
                                                continue;
                                            }
                                            repair_base.push_back(placement);
                                            std::vector<std::vector<int>> sequences;
                                            sequences.push_back(due_order_sequence(problem, sequence_blockers));
                                            sequences.push_back(current_entry_sequence(problem, seed, sequence_blockers));
                                            sequences.push_back(tardy_first_sequence(problem, seed, sequence_blockers));
                                            for (const std::vector<int>& sequence : sequences) {
                                                if (timer.expired(80)) {
                                                    break;
                                                }
                                                for (int bay_mode = 0; bay_mode < 4 && !timer.expired(80); ++bay_mode) {
                                                    for (int position_mode = 0; position_mode < 4 && !timer.expired(80);
                                                         ++position_mode) {
                                                        std::vector<Placement> chained = repair_base;
                                                        const bool old_access = g_use_official_access;
                                                        g_use_official_access = true;
                                                        const bool repaired = repair_sequence_obj1(
                                                            problem,
                                                            chained,
                                                            sequence,
                                                            timer,
                                                            bay_mode,
                                                            position_mode
                                                        );
                                                        const bool feasible_like = repaired && solution_feasible_like(problem, chained);
                                                        g_use_official_access = old_access;
                                                        if (!feasible_like) {
                                                            continue;
                                                        }
                                                        const double obj1 = solution_obj1(problem, chained);
                                                        const double objective = solution_objective(problem, chained);
                                                        if (obj1 + 1e-6 < seed_obj1 &&
                                                            (best_bay.empty() || obj1 + 1e-6 < best_bay_obj1 ||
                                                             (std::abs(obj1 - best_bay_obj1) < 1e-6 &&
                                                              objective + 1e-6 < best_bay_objective))) {
                                                            best_bay = std::move(chained);
                                                            best_bay_obj1 = obj1;
                                                            best_bay_objective = objective;
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    const int primary_count = std::min<int>(static_cast<int>(blockers.size()), 2);
                                    for (int primary_idx = 0; primary_idx < primary_count && !timer.expired(80); ++primary_idx) {
                                        const int primary_blocker = blockers[primary_idx];
                                        std::vector<int> removed{target_id, primary_blocker};
                                        std::vector<Placement> repair_base = remove_blocks(seed, removed);
                                        if (access_time_feasible_against_all(problem, placement, repair_base)) {
                                            repair_base.push_back(placement);
                                            const int blocker_id = primary_blocker;
                                            const Placement original_blocker = find_placement(seed, blocker_id);
                                            const Block& blocker_block = problem.blocks[blocker_id];
                                            const int latest_non_tardy =
                                                std::max(blocker_block.release, blocker_block.due - blocker_block.processing);
                                            for (int blocker_bay = 0; blocker_bay < static_cast<int>(problem.bays.size()) &&
                                                                     !timer.expired(80); ++blocker_bay) {
                                                for (int blocker_orient = 0;
                                                     blocker_orient < static_cast<int>(blocker_block.boxes.size()) &&
                                                     !timer.expired(80);
                                                     ++blocker_orient) {
                                                    int blocker_positions = 0;
                                                    for (const auto& [bx, by] : candidate_positions(
                                                             problem,
                                                             blocker_bay,
                                                             blocker_block.boxes[blocker_orient],
                                                             repair_base,
                                                             3
                                                         )) {
                                                        if (timer.expired(80) || ++blocker_positions > 900) {
                                                            break;
                                                        }
                                                        const int start_end = std::max(latest_non_tardy, original_blocker.entry);
                                                        for (int start = blocker_block.release;
                                                             start <= start_end && !timer.expired(80);
                                                             ++start) {
                                                            Placement blocker;
                                                            blocker.block_id = blocker_id;
                                                            blocker.bay_id = blocker_bay;
                                                            blocker.orient_idx = blocker_orient;
                                                            blocker.x = bx;
                                                            blocker.y = by;
                                                            blocker.entry = start;
                                                            blocker.exit = start + blocker_block.processing;
                                                            blocker.workload = blocker_block.workload;
                                                            if (!access_time_feasible_against_all(problem, blocker, repair_base)) {
                                                                continue;
                                                            }
                                                            std::vector<Placement> chained = repair_base;
                                                            chained.push_back(blocker);
                                                            std::sort(
                                                                chained.begin(),
                                                                chained.end(),
                                                                [](const Placement& a, const Placement& b) {
                                                                    return a.block_id < b.block_id;
                                                                }
                                                            );
                                                            const double obj1 = solution_obj1(problem, chained);
                                                            const double objective = solution_objective(problem, chained);
                                                            if (obj1 + 1e-6 < seed_obj1 &&
                                                                (best_bay.empty() || obj1 + 1e-6 < best_bay_obj1 ||
                                                                 (std::abs(obj1 - best_bay_obj1) < 1e-6 &&
                                                                  objective + 1e-6 < best_bay_objective))) {
                                                                best_bay = std::move(chained);
                                                                best_bay_obj1 = obj1;
                                                                best_bay_objective = objective;
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                            std::vector<int> sequence_blockers{primary_blocker};
                                            std::vector<std::vector<int>> sequences;
                                            sequences.push_back(due_order_sequence(problem, sequence_blockers));
                                            sequences.push_back(current_entry_sequence(problem, seed, sequence_blockers));
                                            for (const std::vector<int>& sequence : sequences) {
                                                if (timer.expired(80)) {
                                                    break;
                                                }
                                                for (int bay_mode = 0; bay_mode < 4 && !timer.expired(80); ++bay_mode) {
                                                    for (int position_mode = 0; position_mode < 4 && !timer.expired(80); ++position_mode) {
                                                        std::vector<Placement> chained = repair_base;
                                                        const bool old_access = g_use_official_access;
                                                        g_use_official_access = true;
                                                        const bool repaired = repair_sequence_obj1(
                                                            problem,
                                                            chained,
                                                            sequence,
                                                            timer,
                                                            bay_mode,
                                                            position_mode
                                                        );
                                                        const bool feasible_like = repaired && solution_feasible_like(problem, chained);
                                                        g_use_official_access = old_access;
                                                        if (!feasible_like) {
                                                            continue;
                                                        }
                                                        const double obj1 = solution_obj1(problem, chained);
                                                        const double objective = solution_objective(problem, chained);
                                                        if (obj1 + 1e-6 < seed_obj1 &&
                                                            (best_bay.empty() || obj1 + 1e-6 < best_bay_obj1 ||
                                                             (std::abs(obj1 - best_bay_obj1) < 1e-6 &&
                                                              objective + 1e-6 < best_bay_objective))) {
                                                            best_bay = std::move(chained);
                                                            best_bay_obj1 = obj1;
                                                            best_bay_objective = objective;
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            continue;
                        }
                        std::vector<Placement> candidate = base;
                        candidate.push_back(placement);
                        std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
                            return a.block_id < b.block_id;
                        });
                        const double obj1 = solution_obj1(problem, candidate);
                        const double objective = solution_objective(problem, candidate);
                        if (obj1 > seed_obj1 + 1e-6 ||
                            (std::abs(obj1 - seed_obj1) < 1e-6 && objective > seed_objective + 1e-6)) {
                            continue;
                        }
                        if (best_bay.empty() || obj1 + 1e-6 < best_bay_obj1 ||
                            (std::abs(obj1 - best_bay_obj1) < 1e-6 &&
                             objective + 1e-6 < best_bay_objective)) {
                            best_bay = candidate;
                            best_bay_obj1 = obj1;
                            best_bay_objective = objective;
                        }
                        if (seed_obj1 <= 10.0 && obj1 + 1e-6 < seed_obj1 &&
                            static_cast<int>(target_candidates.size()) < target_candidate_cap) {
                            target_candidates.push_back(std::move(candidate));
                        }
                    }
                }
                if (!best_bay.empty()) {
                    target_candidates.push_back(std::move(best_bay));
                    if (seed_obj1 > 10.0 && static_cast<int>(target_candidates.size()) >= 4) {
                        break;
                    }
                }
            }
            for (auto& candidate : target_candidates) {
                if (seed_obj1 > 1000.0 && candidate.size() == problem.blocks.size()) {
                    seed_improvements.push_back(candidate);
                }
                add_to_pool(problem, pool, std::move(candidate), max_size);
            }
        }
        if (seed_obj1 > 1000.0 && !seed_improvements.empty() && !timer.expired(80)) {
            std::sort(seed_improvements.begin(), seed_improvements.end(), [&](const auto& a, const auto& b) {
                return obj1_rank_less(problem, a, b);
            });
            struct SingleChange {
                int block_id;
                Placement replacement;
                double obj1;
                double objective;
            };
            std::vector<SingleChange> single_changes;
            single_changes.reserve(seed_improvements.size());
            for (const std::vector<Placement>& candidate : seed_improvements) {
                if (!solution_feasible_like(problem, candidate)) {
                    continue;
                }
                int changed_id = -1;
                int changed_count = 0;
                for (int block_id = 0; block_id < static_cast<int>(problem.blocks.size()); ++block_id) {
                    const Placement before = find_placement(seed, block_id);
                    const Placement after = find_placement(candidate, block_id);
                    if (before.bay_id != after.bay_id || before.x != after.x || before.y != after.y ||
                        before.orient_idx != after.orient_idx || before.entry != after.entry ||
                        before.exit != after.exit) {
                        changed_id = block_id;
                        ++changed_count;
                    }
                }
                if (changed_count != 1 || changed_id < 0) {
                    continue;
                }
                const Placement replacement = find_placement(candidate, changed_id);
                bool duplicate = false;
                for (const SingleChange& existing : single_changes) {
                    if (existing.block_id == changed_id &&
                        existing.replacement.bay_id == replacement.bay_id &&
                        existing.replacement.x == replacement.x &&
                        existing.replacement.y == replacement.y &&
                        existing.replacement.orient_idx == replacement.orient_idx &&
                        existing.replacement.entry == replacement.entry &&
                        existing.replacement.exit == replacement.exit) {
                        duplicate = true;
                        break;
                    }
                }
                if (!duplicate) {
                    single_changes.push_back(
                        SingleChange{
                            changed_id,
                            replacement,
                            solution_obj1(problem, candidate),
                            solution_objective(problem, candidate),
                        }
                    );
                }
            }
            std::sort(single_changes.begin(), single_changes.end(), [](const SingleChange& a, const SingleChange& b) {
                if (std::abs(a.obj1 - b.obj1) > 1e-6) {
                    return a.obj1 < b.obj1;
                }
                if (std::abs(a.objective - b.objective) > 1e-6) {
                    return a.objective < b.objective;
                }
                return a.block_id < b.block_id;
            });

            if (std::getenv("OGC_CPP_ENABLE_BLIND_COMBINE") != nullptr &&
                forced_outputs != nullptr && !single_changes.empty() && !timer.expired(80)) {
                struct ComboState {
                    std::vector<Placement> placements;
                    std::vector<unsigned char> changed;
                    double obj1;
                    double objective;
                };
                std::vector<ComboState> beams;
                beams.push_back(ComboState{
                    seed,
                    std::vector<unsigned char>(problem.blocks.size(), 0),
                    seed_obj1,
                    seed_objective,
                });
                const int source_limit = std::min<int>(static_cast<int>(single_changes.size()), 80);
                for (int source_idx = 0; source_idx < source_limit && !timer.expired(80); ++source_idx) {
                    const SingleChange& change = single_changes[source_idx];
                    std::vector<ComboState> expanded = beams;
                    const int beam_count = std::min<int>(static_cast<int>(beams.size()), 256);
                    for (int beam_idx = 0; beam_idx < beam_count && !timer.expired(80); ++beam_idx) {
                        const ComboState& beam = beams[beam_idx];
                        if (beam.changed[change.block_id]) {
                            continue;
                        }
                        std::vector<Placement> trial = beam.placements;
                        for (Placement& placement : trial) {
                            if (placement.block_id == change.block_id) {
                                placement = change.replacement;
                                break;
                            }
                        }
                        const double trial_obj1 = solution_obj1(problem, trial);
                        if (trial_obj1 > seed_obj1 + 1e-6) {
                            continue;
                        }
                        const double trial_objective = solution_objective(problem, trial);
                        std::vector<unsigned char> trial_changed = beam.changed;
                        trial_changed[change.block_id] = 1;
                        std::vector<Placement> forced = trial;
                        std::sort(forced.begin(), forced.end(), [](const Placement& a, const Placement& b) {
                            return a.block_id < b.block_id;
                        });
                        forced_outputs->push_back(std::move(forced));
                        expanded.push_back(ComboState{
                            std::move(trial),
                            std::move(trial_changed),
                            trial_obj1,
                            trial_objective,
                        });
                    }
                    std::sort(expanded.begin(), expanded.end(), [](const ComboState& a, const ComboState& b) {
                        if (std::abs(a.obj1 - b.obj1) > 1e-6) {
                            return a.obj1 < b.obj1;
                        }
                        return a.objective < b.objective;
                    });
                    if (static_cast<int>(expanded.size()) > 256) {
                        expanded.resize(256);
                    }
                    beams.swap(expanded);
                    if (static_cast<int>(forced_outputs->size()) >= 5000) {
                        break;
                    }
                }
            }

            std::vector<Placement> combined = seed;
            std::vector<int> changed(problem.blocks.size(), 0);
            double combined_obj1 = seed_obj1;
            const int max_sources = std::min<int>(static_cast<int>(seed_improvements.size()), 64);
            int accepted = 0;
            for (int source_idx = 0; source_idx < max_sources && !timer.expired(80); ++source_idx) {
                const std::vector<Placement>& candidate = seed_improvements[source_idx];
                int changed_id = -1;
                int changed_count = 0;
                for (int block_id = 0; block_id < static_cast<int>(problem.blocks.size()); ++block_id) {
                    const Placement before = find_placement(seed, block_id);
                    const Placement after = find_placement(candidate, block_id);
                    if (before.bay_id != after.bay_id || before.x != after.x || before.y != after.y ||
                        before.orient_idx != after.orient_idx || before.entry != after.entry ||
                        before.exit != after.exit) {
                        changed_id = block_id;
                        ++changed_count;
                    }
                }
                if (changed_count != 1 || changed_id < 0 || changed[changed_id]) {
                    continue;
                }
                const Placement replacement = find_placement(candidate, changed_id);
                std::vector<Placement> trial = combined;
                for (Placement& placement : trial) {
                    if (placement.block_id == changed_id) {
                        placement = replacement;
                        break;
                    }
                }
                const double trial_obj1 = solution_obj1(problem, trial);
                if (trial_obj1 + 1e-6 >= combined_obj1) {
                    continue;
                }
                combined.swap(trial);
                combined_obj1 = trial_obj1;
                changed[changed_id] = 1;
                ++accepted;
                add_to_pool(problem, pool, combined, max_size);
                if (forced_outputs != nullptr) {
                    std::vector<Placement> forced = combined;
                    std::sort(forced.begin(), forced.end(), [](const Placement& a, const Placement& b) {
                        return a.block_id < b.block_id;
                    });
                    forced_outputs->push_back(std::move(forced));
                }
                if (accepted >= 16 || combined_obj1 <= 0.0) {
                    break;
                }
            }
        }
    }
}

std::vector<int> release_due_sequence(const Problem& problem, std::vector<int> block_ids) {
    std::sort(block_ids.begin(), block_ids.end(), [&](int a, int b) {
        const Block& ba = problem.blocks[a];
        const Block& bb = problem.blocks[b];
        if (ba.release != bb.release) {
            return ba.release < bb.release;
        }
        if (ba.due != bb.due) {
            return ba.due < bb.due;
        }
        return a < b;
    });
    return block_ids;
}

std::vector<Placement> reschedule_subset_fixed_positions(
    const Problem& problem,
    const std::vector<Placement>& incumbent,
    const std::vector<int>& sequence,
    const Timer& timer
) {
    if (incumbent.size() != problem.blocks.size() || sequence.empty()) {
        return incumbent;
    }

    std::vector<int> selected(problem.blocks.size(), 0);
    for (int block_id : sequence) {
        if (block_id < 0 || block_id >= static_cast<int>(problem.blocks.size())) {
            return incumbent;
        }
        selected[block_id] = 1;
    }

    std::vector<Placement> scheduled;
    scheduled.reserve(incumbent.size());
    for (const Placement& placement : incumbent) {
        if (!selected[placement.block_id]) {
            scheduled.push_back(placement);
        }
    }

    int max_exit = 0;
    for (const Placement& placement : incumbent) {
        max_exit = std::max(max_exit, placement.exit);
    }

    for (int block_id : sequence) {
        if (timer.expired(100)) {
            return incumbent;
        }
        Placement current = find_placement(incumbent, block_id);
        if (current.block_id != block_id) {
            return incumbent;
        }
        const Block& block = problem.blocks[block_id];
        Placement chosen = current;
        bool found = false;
        const int search_end = std::max(max_exit + block.processing + 16, current.exit + block.processing + 16);
        for (int start = block.release; start <= search_end && !timer.expired(100); ++start) {
            Placement candidate = current;
            candidate.entry = start;
            candidate.exit = start + block.processing;
            if (placement_time_feasible_against_all(problem, candidate, scheduled)) {
                chosen = candidate;
                found = true;
                break;
            }
        }
        if (!found) {
            return incumbent;
        }
        scheduled.push_back(chosen);
        max_exit = std::max(max_exit, chosen.exit);
    }

    if (scheduled.size() != problem.blocks.size()) {
        return incumbent;
    }
    std::sort(scheduled.begin(), scheduled.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });
    return scheduled;
}

std::vector<int> bay_blocks_near_tardy(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int bay_id,
    int target_id,
    int max_count
) {
    const Placement target = find_placement(placements, target_id);
    const Block& target_block = problem.blocks[target_id];
    const int window_start = std::max(0, std::min(target.entry, target_block.release) - 8);
    const int window_end = std::max(target.exit, target_block.due) + 14;
    std::vector<std::pair<std::tuple<int, int, int, int>, int>> ranked;
    for (const Placement& other : placements) {
        if (other.bay_id != bay_id) {
            continue;
        }
        const bool overlaps_window = other.entry < window_end && window_start < other.exit;
        const int tardy = std::max(0, other.exit - problem.blocks[other.block_id].due);
        if (!overlaps_window && tardy <= 0 && other.block_id != target_id) {
            continue;
        }
        const int distance = std::min(
            std::abs(other.entry - target.entry),
            std::abs(other.exit - target.exit)
        );
        ranked.push_back(
            {
                std::make_tuple(
                    other.block_id == target_id ? 0 : 1,
                    -tardy,
                    distance,
                    problem.blocks[other.block_id].due
                ),
                other.block_id,
            }
        );
    }
    std::sort(ranked.begin(), ranked.end());
    std::vector<int> result;
    for (const auto& item : ranked) {
        if (std::find(result.begin(), result.end(), item.second) == result.end()) {
            result.push_back(item.second);
        }
        if (static_cast<int>(result.size()) >= max_count) {
            break;
        }
    }
    return result;
}

void bay_schedule_compression_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size
) {
    if (pool.empty() || timer.expired(700)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 8);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    for (const std::vector<Placement>& seed : seeds) {
        if (timer.expired(700)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        if (seed_obj1 <= 0.0 || seed_obj1 > 120.0) {
            continue;
        }
        const double seed_objective = solution_objective(problem, seed);
        const std::vector<int> targets = obj1_repair_targets(problem, seed, 24);
        for (int target_id : targets) {
            if (timer.expired(700)) {
                break;
            }
            const Placement target = find_placement(seed, target_id);
            std::vector<int> all_bay_blocks;
            for (const Placement& placement : seed) {
                if (placement.bay_id == target.bay_id) {
                    all_bay_blocks.push_back(placement.block_id);
                }
            }
            std::vector<int> local_blocks = bay_blocks_near_tardy(
                problem,
                seed,
                target.bay_id,
                target_id,
                std::min<int>(18, std::max<int>(6, static_cast<int>(all_bay_blocks.size())))
            );
            std::vector<std::vector<int>> subsets;
            subsets.push_back(local_blocks);
            if (all_bay_blocks.size() <= 80) {
                subsets.push_back(all_bay_blocks);
            }

            for (std::vector<int> subset : subsets) {
                if (timer.expired(700) || subset.empty()) {
                    break;
                }
                if (std::find(subset.begin(), subset.end(), target_id) == subset.end()) {
                    subset.push_back(target_id);
                }
                std::sort(subset.begin(), subset.end());
                subset.erase(std::unique(subset.begin(), subset.end()), subset.end());

                std::vector<std::vector<int>> sequences;
                sequences.push_back(due_order_sequence(problem, subset));
                sequences.push_back(release_due_sequence(problem, subset));
                sequences.push_back(current_entry_sequence(problem, seed, subset));
                sequences.push_back(tardy_first_sequence(problem, seed, subset));

                for (const std::vector<int>& sequence : sequences) {
                    if (timer.expired(700)) {
                        break;
                    }
                    std::vector<Placement> candidate = reschedule_subset_fixed_positions(
                        problem,
                        seed,
                        sequence,
                        timer
                    );
                    const double obj1 = solution_obj1(problem, candidate);
                    if (obj1 + 1e-6 > seed_obj1) {
                        continue;
                    }
                    const double objective = solution_objective(problem, candidate);
                    if (obj1 + 1e-6 < seed_obj1 ||
                        (std::abs(obj1 - seed_obj1) < 1e-6 &&
                         objective + 1e-6 < seed_objective)) {
                        add_to_pool(problem, pool, std::move(candidate), max_size);
                    }
                }
            }
        }
    }
}

int required_delay_entry_for_access(
    const Problem& problem,
    const std::vector<Placement>& placements,
    int block_id
) {
    const Placement& target = placements[block_id];
    const Block& block = problem.blocks[block_id];
    int delayed_entry = std::max(target.entry, block.release);
    if (target.exit - target.entry < block.processing) {
        delayed_entry = std::max(delayed_entry, target.entry + block.processing - (target.exit - target.entry));
    }
    for (const Placement& other : placements) {
        if (other.block_id == target.block_id || other.bay_id != target.bay_id) {
            continue;
        }
        if (target.entry < other.exit && other.entry < target.exit &&
            same_height_collision(target, other, problem)) {
            delayed_entry = std::max(delayed_entry, other.exit);
            continue;
        }
        if (other.entry < target.entry && target.entry < other.exit &&
            crane_path_obstructed(target, other, problem)) {
            delayed_entry = std::max(delayed_entry, other.exit);
            continue;
        }
        if (other.entry < target.exit && target.exit < other.exit &&
            crane_path_obstructed(target, other, problem)) {
            delayed_entry = std::max(delayed_entry, other.exit);
            continue;
        }
        if (target.entry < other.entry && other.entry < target.exit &&
            crane_path_obstructed(other, target, problem)) {
            delayed_entry = std::max(delayed_entry, other.exit);
            continue;
        }
        if (target.entry < other.exit && other.exit < target.exit &&
            crane_path_obstructed(other, target, problem)) {
            delayed_entry = std::max(delayed_entry, other.exit);
        }
    }
    return delayed_entry;
}

std::vector<Placement> access_delay_repair_solution(
    const Problem& problem,
    std::vector<Placement> candidate,
    const Timer& timer
) {
    if (candidate.size() != problem.blocks.size() || !problem_has_polygon_payload(problem)) {
        return candidate;
    }
    std::sort(candidate.begin(), candidate.end(), [](const Placement& a, const Placement& b) {
        return a.block_id < b.block_id;
    });
    for (int pass = 0; pass < 120 && !timer.expired(90); ++pass) {
        int chosen_id = -1;
        int chosen_entry = 0;
        int chosen_delta = std::numeric_limits<int>::max();
        for (int block_id = 0; block_id < static_cast<int>(candidate.size()); ++block_id) {
            if (candidate[block_id].block_id != block_id) {
                return candidate;
            }
            const int delayed_entry = required_delay_entry_for_access(problem, candidate, block_id);
            if (delayed_entry <= candidate[block_id].entry) {
                continue;
            }
            const int delta = delayed_entry - candidate[block_id].entry;
            const int tardy = std::max(0, candidate[block_id].exit - problem.blocks[block_id].due);
            if (chosen_id < 0 || std::make_tuple(tardy > 0 ? 1 : 0, delta, block_id) <
                                     std::make_tuple(
                                         std::max(0, candidate[chosen_id].exit - problem.blocks[chosen_id].due) > 0 ? 1 : 0,
                                         chosen_delta,
                                         chosen_id
                                     )) {
                chosen_id = block_id;
                chosen_entry = delayed_entry;
                chosen_delta = delta;
            }
        }
        if (chosen_id < 0) {
            break;
        }
        candidate[chosen_id].entry = chosen_entry;
        candidate[chosen_id].exit = chosen_entry + problem.blocks[chosen_id].processing;
    }
    return candidate;
}

void access_delay_repair_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size
) {
    if (pool.empty() || !problem_has_polygon_payload(problem) || timer.expired(700)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 12);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }
    for (auto seed : seeds) {
        if (timer.expired(700)) {
            break;
        }
        const double seed_obj1 = solution_obj1(problem, seed);
        if (seed_obj1 > 120.0) {
            continue;
        }
        std::vector<Placement> repaired = access_delay_repair_solution(problem, std::move(seed), timer);
        if (repaired.size() != problem.blocks.size()) {
            continue;
        }
        if (solution_obj1(problem, repaired) <= seed_obj1 + 40.0) {
            add_to_pool(problem, pool, std::move(repaired), max_size);
        }
    }
}

void post_improve_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size
) {
    if (pool.empty() || timer.expired(550)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });

    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 4);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    for (const auto& seed : seeds) {
        if (timer.expired(550)) {
            break;
        }
        for (int bay_mode = 0; bay_mode < 4 && !timer.expired(550); ++bay_mode) {
            for (int position_mode = 0; position_mode < 3 && !timer.expired(550); ++position_mode) {
                std::vector<Placement> candidate = seed;
                pair_relocate_pass(problem, candidate, timer, bay_mode, position_mode);
                reschedule_pass(problem, candidate, timer);
                left_shift_obj1_pass(problem, candidate, timer);
                relaxed_left_shift_obj1_pass(problem, candidate, timer);
                obj1_reschedule_pass(problem, candidate, timer);
                if (!timer.expired(550)) {
                    relocate_pass(problem, candidate, timer, bay_mode, position_mode);
                    reschedule_pass(problem, candidate, timer);
                    left_shift_obj1_pass(problem, candidate, timer);
                    relaxed_left_shift_obj1_pass(problem, candidate, timer);
                    obj1_reschedule_pass(problem, candidate, timer);
                }
                if (!timer.expired(550)) {
                    preference_relocate_pass(problem, candidate, timer, position_mode);
                    reschedule_pass(problem, candidate, timer);
                    left_shift_obj1_pass(problem, candidate, timer);
                    relaxed_left_shift_obj1_pass(problem, candidate, timer);
                    obj1_reschedule_pass(problem, candidate, timer);
                }
                add_to_pool(problem, pool, std::move(candidate), max_size);
            }
        }
    }
}

void alns_improve_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size
) {
    if (pool.empty() || timer.expired(700)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });

    const int seed_count = std::min<int>(static_cast<int>(pool.size()), 1);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    Rng rng(1469598103934665603ULL + static_cast<uint64_t>(problem.blocks.size()) * 1099511628211ULL);
    int iteration = 0;
    for (auto seed : seeds) {
        if (timer.expired(700)) {
            break;
        }
        std::vector<Placement> current = std::move(seed);
        double best_objective = solution_objective(problem, current);
        int stale = 0;
        while (!timer.expired(700) && stale < 300) {
            std::vector<Placement> candidate = current;
            const bool improved = alns_repair_iteration(problem, candidate, timer, iteration, rng);
            if (!timer.expired(700)) {
                left_shift_obj1_pass(problem, candidate, timer);
                relaxed_left_shift_obj1_pass(problem, candidate, timer);
                obj1_reschedule_pass(problem, candidate, timer);
            }
            ++iteration;
            if (improved) {
                const double objective = solution_objective(problem, candidate);
                if (objective + 1e-6 < best_objective) {
                    current.swap(candidate);
                    best_objective = objective;
                    stale = 0;
                    add_to_pool(problem, pool, current, max_size);
                    continue;
                }
            }
            ++stale;
        }
        add_to_pool(problem, pool, std::move(current), max_size);
    }
}

void bounded_parallel_alns_pool(
    const Problem& problem,
    std::vector<std::vector<Placement>>& pool,
    const Timer& timer,
    int max_size,
    int seed_limit,
    int stale_limit,
    int max_iterations,
    int reserve_ms
) {
    if (pool.empty() || seed_limit <= 0 || max_iterations <= 0 || timer.expired(reserve_ms)) {
        return;
    }
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });

    const int seed_count = std::min<int>(static_cast<int>(pool.size()), seed_limit);
    std::vector<std::vector<Placement>> seeds;
    seeds.reserve(seed_count);
    for (int i = 0; i < seed_count; ++i) {
        seeds.push_back(pool[i]);
    }

    struct SeedResult {
        std::vector<std::vector<Placement>> candidates;
    };
    std::vector<SeedResult> results(seed_count);
    std::atomic<int> next_seed(0);

    int requested_threads = 0;
    if (const char* env_threads = std::getenv("OGC_CPP_THREADS")) {
        requested_threads = std::atoi(env_threads);
    }
    const int hardware_threads = std::max(1, static_cast<int>(std::thread::hardware_concurrency()));
    const int default_threads = problem.blocks.size() >= 200 ? 4 : 2;
    const int worker_count = std::max(
        1,
        std::min(
            seed_count,
            requested_threads > 0 ? std::min(requested_threads, 16) : std::min(hardware_threads, default_threads)
        )
    );

    auto worker = [&]() {
        while (true) {
            const int seed_index = next_seed.fetch_add(1);
            if (seed_index >= seed_count || timer.expired(reserve_ms)) {
                break;
            }
            std::vector<Placement> current = seeds[seed_index];
            std::vector<Placement> best = current;
            double current_obj1 = solution_obj1(problem, current);
            double current_objective = solution_objective(problem, current);
            double best_obj1 = solution_obj1(problem, current);
            double best_objective = solution_objective(problem, current);
            int stale = 0;
            int iteration = seed_index * 9973;
            Rng rng(
                1469598103934665603ULL ^
                (static_cast<uint64_t>(problem.blocks.size()) * 1099511628211ULL) ^
                (static_cast<uint64_t>(seed_index + 1) * 780291637ULL)
            );
            for (int local_iter = 0;
                 local_iter < max_iterations && stale < stale_limit && !timer.expired(reserve_ms);
                 ++local_iter, ++iteration) {
                if (timer.remaining_ms() <= reserve_ms + 450) {
                    break;
                }
                std::vector<Placement> candidate;
                bool generated = alns_neighbor_iteration(problem, current, candidate, timer, iteration, rng);
                if (!generated && problem.blocks.size() >= 150 && local_iter % 12 == 11 &&
                    timer.remaining_ms() > reserve_ms + 900) {
                    candidate = current;
                    generated = entry_slot_chain_repair_iteration(problem, candidate, timer, iteration);
                }
                if (!generated) {
                    ++stale;
                    continue;
                }
                const double candidate_obj1 = solution_obj1(problem, candidate);
                const double candidate_objective = solution_objective(problem, candidate);
                const bool improves_best =
                    candidate_obj1 + 1e-6 < best_obj1 ||
                    (std::abs(candidate_obj1 - best_obj1) < 1e-6 &&
                     candidate_objective + 1e-6 < best_objective);
                const bool improves_current =
                    candidate_obj1 + 1e-6 < current_obj1 ||
                    (std::abs(candidate_obj1 - current_obj1) < 1e-6 &&
                     candidate_objective + 1e-6 < current_objective);
                const bool accept =
                    improves_current ||
                    accept_worse_alns_move(
                        problem,
                        current_obj1,
                        current_objective,
                        candidate_obj1,
                        candidate_objective,
                        best_obj1,
                        local_iter,
                        max_iterations,
                        rng
                    );
                if (improves_best) {
                    best = candidate;
                    best_obj1 = candidate_obj1;
                    best_objective = candidate_objective;
                    results[seed_index].candidates.push_back(best);
                    stale = 0;
                } else {
                    ++stale;
                }
                if (accept) {
                    current.swap(candidate);
                    current_obj1 = candidate_obj1;
                    current_objective = candidate_objective;
                    if (!improves_best && stale > 0) {
                        --stale;
                    }
                }
            }
            results[seed_index].candidates.push_back(std::move(best));
        }
    };

    if (worker_count <= 1) {
        worker();
    } else {
        std::vector<std::thread> workers;
        workers.reserve(worker_count);
        for (int worker_index = 0; worker_index < worker_count; ++worker_index) {
            workers.emplace_back(worker);
        }
        for (std::thread& thread : workers) {
            thread.join();
        }
    }

    for (SeedResult& result : results) {
        for (auto& candidate : result.candidates) {
            add_to_pool(problem, pool, std::move(candidate), max_size);
        }
    }
}

std::vector<std::vector<Placement>> solve_pool(const Problem& problem, const Timer& timer) {
    std::vector<std::vector<Placement>> pool;
    const int output_pool_cap = problem.initial_seeds.empty() ? 28 : 80;
    auto run_mode = [&](int overlap_mode, int max_pool_size, int reserve_ms) {
        g_overlap_mode = overlap_mode;

        struct TaskResult {
            std::vector<std::vector<Placement>> candidates;
        };

        std::vector<std::tuple<int, int, int>> tasks;
        tasks.reserve(72);
        const int order_mode_count = (problem.blocks.size() >= 200 && problem.w1 < 1000.0) ? 8 : 6;
        for (int order_mode = 0; order_mode < order_mode_count; ++order_mode) {
            for (int bay_mode = 0; bay_mode < 4; ++bay_mode) {
                for (int position_mode = 0; position_mode < 3; ++position_mode) {
                    tasks.emplace_back(order_mode, bay_mode, position_mode);
                }
            }
        }

        std::vector<TaskResult> task_results(tasks.size());
        std::atomic<int> next_task(0);
        int requested_threads = 0;
        if (const char* env_threads = std::getenv("OGC_CPP_THREADS")) {
            requested_threads = std::atoi(env_threads);
        }
        const int hardware_threads = std::max(1, static_cast<int>(std::thread::hardware_concurrency()));
        const int default_threads = problem.blocks.size() >= 200 ? 4 : 2;
        const int worker_count = std::max(
            1,
            std::min(
                static_cast<int>(tasks.size()),
                requested_threads > 0 ? std::min(requested_threads, 16) : std::min(hardware_threads, default_threads)
            )
        );

        auto worker = [&]() {
            while (true) {
                const int task_index = next_task.fetch_add(1);
                if (task_index >= static_cast<int>(tasks.size()) || timer.expired(reserve_ms)) {
                    break;
                }
                const auto [order_mode, bay_mode, position_mode] = tasks[task_index];
                std::vector<Placement> candidate = construct_solution(problem, timer, order_mode, bay_mode, position_mode);
                if (candidate.size() != problem.blocks.size()) {
                    continue;
                }
                reschedule_pass(problem, candidate, timer);
                left_shift_obj1_pass(problem, candidate, timer);
                relaxed_left_shift_obj1_pass(problem, candidate, timer);
                obj1_reschedule_pass(problem, candidate, timer);
                task_results[task_index].candidates.push_back(candidate);
                if (!timer.expired(std::max(120, reserve_ms))) {
                    relocate_pass(problem, candidate, timer, bay_mode, position_mode);
                    reschedule_pass(problem, candidate, timer);
                    left_shift_obj1_pass(problem, candidate, timer);
                    relaxed_left_shift_obj1_pass(problem, candidate, timer);
                    obj1_reschedule_pass(problem, candidate, timer);
                }
                if (!timer.expired(std::max(120, reserve_ms))) {
                    preference_relocate_pass(problem, candidate, timer, position_mode);
                    reschedule_pass(problem, candidate, timer);
                    left_shift_obj1_pass(problem, candidate, timer);
                    relaxed_left_shift_obj1_pass(problem, candidate, timer);
                    obj1_reschedule_pass(problem, candidate, timer);
                }
                task_results[task_index].candidates.push_back(std::move(candidate));
            }
        };

        if (worker_count <= 1) {
            worker();
        } else {
            std::vector<std::thread> workers;
            workers.reserve(worker_count);
            for (int worker_index = 0; worker_index < worker_count; ++worker_index) {
                workers.emplace_back(worker);
            }
            for (std::thread& thread : workers) {
                thread.join();
            }
        }

        std::vector<Placement> best;
        double best_objective = std::numeric_limits<double>::infinity();
        double best_obj1 = std::numeric_limits<double>::infinity();
        std::vector<std::vector<Placement>> local_pool;
        for (TaskResult& task_result : task_results) {
            for (auto& candidate : task_result.candidates) {
                const double objective = solution_objective(problem, candidate);
                const double obj1 = solution_obj1(problem, candidate);
                if (obj1 + 1e-6 < best_obj1 ||
                    (std::abs(obj1 - best_obj1) < 1e-6 && objective < best_objective)) {
                    best_obj1 = obj1;
                    best_objective = objective;
                    best = candidate;
                }
                add_to_pool(problem, local_pool, std::move(candidate), max_pool_size);
            }
        }

        if (best.empty()) {
            best = construct_solution(problem, timer, 0, 0, 0);
        }
        add_to_pool(problem, local_pool, best, max_pool_size);
        for (const auto& candidate : local_pool) {
            add_to_pool(problem, pool, candidate, output_pool_cap);
        }
    };

    if (problem.blocks.size() >= 150 && !timer.expired(900)) {
        std::vector<Placement> release_seed = construct_release_batch_seed(problem, timer);
        if (release_seed.size() == problem.blocks.size()) {
            reschedule_pass(problem, release_seed, timer);
            left_shift_obj1_pass(problem, release_seed, timer);
            relaxed_left_shift_obj1_pass(problem, release_seed, timer);
            obj1_reschedule_pass(problem, release_seed, timer);
            add_to_pool(problem, pool, release_seed, 28);
            add_to_pool(problem, pool, std::move(release_seed), 28);
        }
    }

    if (!problem.initial_seeds.empty() && !timer.expired(120)) {
        bool large_tardy_seed = false;
        bool high_w1_small_tail_seed = false;
        bool high_w1_medium_tail_seed = false;
        for (const auto& seed : problem.initial_seeds) {
            const double seed_obj1 = solution_obj1(problem, seed);
            if (seed_obj1 > 1000.0) {
                large_tardy_seed = true;
            }
            if (problem.w1 >= 1000.0 && seed_obj1 > 0.0 &&
                seed_obj1 <= (problem.blocks.size() <= 140 ? 60.0 : 10.0)) {
                high_w1_small_tail_seed = true;
            }
            if (problem.w1 >= 1000.0 && problem.blocks.size() <= 140 &&
                seed_obj1 > 10.0 && seed_obj1 <= 1000.0) {
                high_w1_medium_tail_seed = true;
            }
            add_to_pool(problem, pool, seed, output_pool_cap);
        }
        const int seed_pool_cap = large_tardy_seed ? 240 : output_pool_cap;
        std::vector<std::vector<Placement>> forced_outputs;
        if (high_w1_small_tail_seed && !timer.expired(900)) {
            for (int pair_pass = 0; pair_pass < 2 && !timer.expired(900); ++pair_pass) {
                high_w1_pair_blocker_repair_pool(problem, pool, timer, seed_pool_cap, forced_outputs);
            }
        }
        const bool high_w1_blocker_push_seed =
            high_w1_small_tail_seed ||
            (high_w1_medium_tail_seed && problem.bays.size() >= 3 && problem.w3 <= 200.0);
        if (high_w1_blocker_push_seed && !timer.expired(900)) {
            high_w1_small_tail_blocker_push_pool(problem, pool, timer, seed_pool_cap, forced_outputs);
        }
        if ((high_w1_small_tail_seed ||
             (high_w1_medium_tail_seed && (problem.bays.size() >= 3 || problem.w3 <= 200.0))) &&
            !timer.expired(900)) {
            high_w1_small_tail_option_repack_pool(problem, pool, timer, seed_pool_cap, forced_outputs);
        }
        if (high_w1_small_tail_seed && !timer.expired(900)) {
            high_w1_small_tail_cluster_pool(problem, pool, timer, seed_pool_cap, forced_outputs);
        }
        if ((high_w1_small_tail_seed || high_w1_medium_tail_seed) && !timer.expired(900)) {
            high_w1_local_crossbay_subset_pool(problem, pool, timer, seed_pool_cap, forced_outputs);
        }
        if (high_w1_medium_tail_seed && !timer.expired(900)) {
            high_w1_single_grid_ontime_pool(problem, pool, timer, seed_pool_cap, forced_outputs);
        }
        if (high_w1_medium_tail_seed && !timer.expired(900)) {
            high_w1_all_tardy_crossbay_pool(problem, pool, timer, seed_pool_cap, forced_outputs);
        }
        if (high_w1_medium_tail_seed && !timer.expired(900)) {
            high_w1_random_tardy_cluster_pool(problem, pool, timer, seed_pool_cap, forced_outputs);
        }
        if (large_tardy_seed && problem.blocks.size() >= 250 &&
            problem.bays.size() <= 4 && problem.w1 < 1000.0 &&
            (problem.time_ms >= 18000 || std::getenv("OGC_CPP_FOCUSED_LATE_SHIFT") != nullptr) &&
            !timer.expired(120)) {
            std::vector<std::vector<Placement>> shift_sources = problem.initial_seeds;
            if (std::getenv("OGC_CPP_FOCUSED_LATE_SHIFT") == nullptr) {
                std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
                    return obj1_rank_less(problem, a, b);
                });
                const int extra_shift_sources = std::min<int>(static_cast<int>(pool.size()), 2);
                for (int i = 0; i < extra_shift_sources; ++i) {
                    shift_sources.push_back(pool[i]);
                }
            }
            same_position_shift_sample_pool(problem, shift_sources, pool, timer, seed_pool_cap, forced_outputs);
            if (std::getenv("OGC_CPP_FOCUSED_LATE_SHIFT") != nullptr) {
                std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
                    return obj1_rank_less(problem, a, b);
                });
                if (!forced_outputs.empty()) {
                    for (auto& candidate : forced_outputs) {
                        if (candidate.size() == problem.blocks.size()) {
                            pool.push_back(std::move(candidate));
                        }
                    }
                }
                return pool;
            }
        }
        const int due_window_passes = 1;
        for (int pass = 0; pass < due_window_passes && !timer.expired(120); ++pass) {
            due_window_seed_relocate_pool(
                problem,
                pool,
                timer,
                seed_pool_cap,
                large_tardy_seed ? &forced_outputs : nullptr
            );
        }
        if (!forced_outputs.empty()) {
            std::sort(forced_outputs.begin(), forced_outputs.end(), [&](const auto& a, const auto& b) {
                return obj1_rank_less(problem, a, b);
            });
            const int forced_seed_limit = large_tardy_seed ? 16 : 8;
            int forced_seed_count = 0;
            for (const auto& candidate : forced_outputs) {
                if (candidate.size() == problem.blocks.size()) {
                    add_to_pool(problem, pool, candidate, seed_pool_cap);
                    ++forced_seed_count;
                    if (forced_seed_count >= forced_seed_limit) {
                        break;
                    }
                }
            }
        }
        bay_schedule_compression_pool(problem, pool, timer, seed_pool_cap);
        if (large_tardy_seed && problem.blocks.size() >= 250 && !timer.expired(3000)) {
            const bool low_w1_large = problem.w1 < 1000.0;
            bounded_parallel_alns_pool(
                problem,
                pool,
                timer,
                seed_pool_cap,
                low_w1_large ? 3 : 4,
                low_w1_large ? 14 : 22,
                low_w1_large ? 24 : 40,
                low_w1_large ? 3200 : 2600
            );
            bay_schedule_compression_pool(problem, pool, timer, seed_pool_cap);
        }
        if (high_w1_medium_tail_seed && !timer.expired(900)) {
            obj1_first_repair_pool(problem, pool, timer, seed_pool_cap);
            bay_schedule_compression_pool(problem, pool, timer, seed_pool_cap);
        }
        std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
            return obj1_rank_less(problem, a, b);
        });
        if (!forced_outputs.empty()) {
            for (auto& candidate : forced_outputs) {
                if (candidate.size() == problem.blocks.size()) {
                    pool.push_back(std::move(candidate));
                }
            }
        }
        return pool;
    }

    const bool prefer_due_spacing = problem.blocks.size() < 300;
    g_due_spacing_weight = prefer_due_spacing ? 0.15 : 0.0;
    run_mode(0, 10, 180);
    if (!timer.expired(900)) {
        g_due_spacing_weight = prefer_due_spacing ? 0.0 : 0.15;
        run_mode(0, 10, 180);
    }
    if (problem.w1 >= 1000.0 && problem.blocks.size() <= 140 && !pool.empty() && !timer.expired(900)) {
        bool high_w1_small_tail_pool = false;
        bool high_w1_medium_tail_pool = false;
        for (const auto& seed : pool) {
            const double seed_obj1 = solution_obj1(problem, seed);
            if (seed_obj1 > 0.0 && seed_obj1 <= (problem.blocks.size() <= 140 ? 60.0 : 10.0)) {
                high_w1_small_tail_pool = true;
            }
            if (seed_obj1 > 10.0 && seed_obj1 <= 1000.0) {
                high_w1_medium_tail_pool = true;
            }
        }

        const int cxx_seed_pool_cap = 80;
        std::vector<std::vector<Placement>> forced_outputs;
        if (high_w1_small_tail_pool && !timer.expired(900)) {
            for (int pair_pass = 0; pair_pass < 2 && !timer.expired(900); ++pair_pass) {
                high_w1_pair_blocker_repair_pool(problem, pool, timer, cxx_seed_pool_cap, forced_outputs);
            }
        }
        const bool high_w1_blocker_push_pool =
            high_w1_small_tail_pool ||
            (high_w1_medium_tail_pool && problem.bays.size() >= 3 && problem.w3 <= 200.0);
        if (high_w1_blocker_push_pool && !timer.expired(900)) {
            high_w1_small_tail_blocker_push_pool(problem, pool, timer, cxx_seed_pool_cap, forced_outputs);
        }
        if ((high_w1_small_tail_pool ||
             (high_w1_medium_tail_pool && (problem.bays.size() >= 3 || problem.w3 <= 200.0))) &&
            !timer.expired(900)) {
            high_w1_small_tail_option_repack_pool(problem, pool, timer, cxx_seed_pool_cap, forced_outputs);
        }
        if (high_w1_small_tail_pool && !timer.expired(900)) {
            high_w1_small_tail_cluster_pool(problem, pool, timer, cxx_seed_pool_cap, forced_outputs);
        }
        if ((high_w1_small_tail_pool || high_w1_medium_tail_pool) && !timer.expired(900)) {
            high_w1_local_crossbay_subset_pool(problem, pool, timer, cxx_seed_pool_cap, forced_outputs);
        }
        if (high_w1_medium_tail_pool && !timer.expired(900)) {
            high_w1_single_grid_ontime_pool(problem, pool, timer, cxx_seed_pool_cap, forced_outputs);
        }
        if (high_w1_medium_tail_pool && !timer.expired(900)) {
            high_w1_all_tardy_crossbay_pool(problem, pool, timer, cxx_seed_pool_cap, forced_outputs);
        }
        if (high_w1_medium_tail_pool && !timer.expired(900)) {
            high_w1_random_tardy_cluster_pool(problem, pool, timer, cxx_seed_pool_cap, forced_outputs);
        }
        for (auto& candidate : forced_outputs) {
            if (candidate.size() == problem.blocks.size()) {
                add_to_pool(problem, pool, std::move(candidate), cxx_seed_pool_cap);
            }
        }
        bay_schedule_compression_pool(problem, pool, timer, cxx_seed_pool_cap);
    }
    if (!timer.expired(900)) {
        post_improve_pool(problem, pool, timer, 14);
        bay_schedule_compression_pool(problem, pool, timer, 14);
    }
    if (!timer.expired(900)) {
        obj1_first_repair_pool(problem, pool, timer, 14);
        bay_schedule_compression_pool(problem, pool, timer, 14);
    }
    if (!timer.expired(1200)) {
        alns_improve_pool(problem, pool, timer, 14);
    }
    if (!timer.expired(900)) {
        obj1_first_repair_pool(problem, pool, timer, 14);
        bay_schedule_compression_pool(problem, pool, timer, 14);
    }
    if (!timer.expired(500)) {
        g_due_spacing_weight = 0.0;
        run_mode(2, 4, 120);
    }
    g_overlap_mode = 0;
    g_due_spacing_weight = 0.0;
    std::sort(pool.begin(), pool.end(), [&](const auto& a, const auto& b) {
        return obj1_rank_less(problem, a, b);
    });
    return pool;
}

}  // namespace

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);

    Problem problem;
    if (!read_problem(problem)) {
        std::cout << "ERR input\n";
        return 2;
    }
    g_use_official_access = false;
    bool has_polygon_payload = false;
    for (const Block& block : problem.blocks) {
        for (const auto& orientation_layers : block.layer_polygons) {
            if (!orientation_layers.empty()) {
                has_polygon_payload = true;
                break;
            }
        }
        if (has_polygon_payload) {
            break;
        }
    }
    g_filter_feasible_like = false;
    g_use_official_access = has_polygon_payload && std::getenv("OGC_CPP_STRICT_POLYGON_ACCESS") != nullptr;
    Timer timer(problem.time_ms);
    const std::vector<std::vector<Placement>> pool = solve_pool(problem, timer);
    if (pool.empty() || pool.front().size() != problem.blocks.size()) {
        std::cout << "ERR solve\n";
        return 3;
    }

    std::cout << "OK_MULTI " << pool.size() << ' ' << problem.blocks.size() << "\n";
    for (const auto& placements : pool) {
        for (const Placement& p : placements) {
            std::cout << p.block_id << ' ' << p.bay_id << ' ' << p.x << ' ' << p.y << ' '
                      << p.orient_idx << ' ' << p.entry << ' ' << p.exit << "\n";
        }
    }
    return 0;
}
