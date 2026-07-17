#include <ompl/base/PlannerData.h>
#include <ompl/base/SpaceInformation.h>
#include <ompl/base/objectives/MinimizeArrivalTime.h>
#include <ompl/base/spaces/RealVectorStateSpace.h>
#include <ompl/base/spaces/SpaceTimeStateSpace.h>
#include <ompl/geometric/PathGeometric.h>
#include <ompl/geometric/SimpleSetup.h>
#include <ompl/geometric/planners/rrt/STRRTstar.h>
#include <ompl/util/RandomNumbers.h>
#include <ompl/config.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace ob = ompl::base;
namespace og = ompl::geometric;

namespace
{
    constexpr double kPi = 3.14159265358979323846;

    struct Options
    {
        double solveTime = 2.0;
        double vMax = 0.75;
        double maxTime = 8.0;
        double obstacleRadius = 0.18;
        std::uint_fast32_t seed = 7;
        std::string output;
    };

    struct PathPoint
    {
        double x;
        double y;
        double t;
    };

    Options parseOptions(int argc, char **argv)
    {
        Options options;
        for (int index = 1; index < argc; ++index)
        {
            const std::string argument = argv[index];
            const auto requireValue = [&]() -> std::string {
                if (index + 1 >= argc)
                    throw std::runtime_error("missing value after " + argument);
                return argv[++index];
            };

            if (argument == "--solve-time")
                options.solveTime = std::stod(requireValue());
            else if (argument == "--v-max")
                options.vMax = std::stod(requireValue());
            else if (argument == "--max-time")
                options.maxTime = std::stod(requireValue());
            else if (argument == "--obstacle-radius")
                options.obstacleRadius = std::stod(requireValue());
            else if (argument == "--seed")
                options.seed = static_cast<std::uint_fast32_t>(std::stoul(requireValue()));
            else if (argument == "--output")
                options.output = requireValue();
            else if (argument == "--help")
            {
                std::cout << "Native OMPL STRRTstar demo\n"
                          << "  --solve-time SEC       planner time limit (default 2.0)\n"
                          << "  --v-max SPEED          maximum spatial speed (default 0.75)\n"
                          << "  --max-time SEC         space-time upper bound (default 8.0)\n"
                          << "  --obstacle-radius R    moving obstacle radius (default 0.18)\n"
                          << "  --seed N               OMPL random seed (default 7)\n"
                          << "  --output FILE          write result JSON\n";
                std::exit(0);
            }
            else
                throw std::runtime_error("unknown argument: " + argument);
        }

        if (options.solveTime <= 0.0 || options.vMax <= 0.0 || options.maxTime <= 0.0 ||
            options.obstacleRadius < 0.0)
            throw std::runtime_error("time, speed and radius arguments must be positive");
        return options;
    }

    PathPoint pointFromState(const ob::State *state)
    {
        const auto *compound = state->as<ob::CompoundState>();
        const auto *position = compound->as<ob::RealVectorStateSpace::StateType>(0);
        const auto *time = compound->as<ob::TimeStateSpace::StateType>(1);
        return {position->values[0], position->values[1], time->position};
    }

    std::pair<double, double> obstacleCenter(double time)
    {
        // The obstacle oscillates vertically and therefore occupies different
        // spatial cells at different planning times.
        return {0.5, 0.5 + 0.18 * std::sin(2.0 * kPi * time / 4.0)};
    }

    class DynamicObstacleValidityChecker
    {
    public:
        explicit DynamicObstacleValidityChecker(double radius) : radius_(radius)
        {
        }

        bool operator()(const ob::State *state) const
        {
            const auto point = pointFromState(state);
            const auto center = obstacleCenter(point.t);
            const double dx = point.x - center.first;
            const double dy = point.y - center.second;
            return dx * dx + dy * dy > radius_ * radius_;
        }

    private:
        double radius_;
    };

    class SpaceTimeMotionValidator final : public ob::MotionValidator
    {
    public:
        explicit SpaceTimeMotionValidator(const ob::SpaceInformationPtr &spaceInformation)
          : ob::MotionValidator(spaceInformation)
          , space_(spaceInformation->getStateSpace()->as<ob::SpaceTimeStateSpace>())
          , vMax_(space_->getVMax())
        {
        }

        bool checkMotion(const ob::State *from, const ob::State *to) const override
        {
            if (!si_->isValid(to))
            {
                ++invalid_;
                return false;
            }

            const double deltaTime = ob::SpaceTimeStateSpace::getStateTime(to) -
                                     ob::SpaceTimeStateSpace::getStateTime(from);
            const double spatialDistance = space_->distanceSpace(from, to);
            if (deltaTime <= 0.0 || spatialDistance / deltaTime > vMax_ + 1e-9)
            {
                ++invalid_;
                return false;
            }

            const auto spatialSegments = static_cast<unsigned int>(std::ceil(spatialDistance / 0.015));
            const auto timeSegments = static_cast<unsigned int>(std::ceil(deltaTime / 0.04));
            const unsigned int segmentCount = std::max(2U, std::max(spatialSegments, timeSegments));
            ob::State *interpolated = si_->allocState();
            bool valid = true;
            for (unsigned int segment = 1; segment < segmentCount; ++segment)
            {
                space_->interpolate(from, to, static_cast<double>(segment) / segmentCount, interpolated);
                if (!si_->isValid(interpolated))
                {
                    valid = false;
                    break;
                }
            }
            si_->freeState(interpolated);

            if (valid)
                ++valid_;
            else
                ++invalid_;
            return valid;
        }

        bool checkMotion(const ob::State *from, const ob::State *to,
                         std::pair<ob::State *, double> &lastValid) const override
        {
            const bool valid = checkMotion(from, to);
            lastValid.second = valid ? 1.0 : 0.0;
            if (lastValid.first != nullptr)
                si_->copyState(lastValid.first, valid ? to : from);
            return valid;
        }

    private:
        ob::SpaceTimeStateSpace *space_;
        double vMax_;
    };

    void writeJson(const std::filesystem::path &output, const Options &options, const ob::PlannerStatus &status,
                   double planningSeconds, unsigned int exploredStates, const std::vector<PathPoint> &path,
                   double spatialLength, double maximumSegmentSpeed)
    {
        if (!output.parent_path().empty())
            std::filesystem::create_directories(output.parent_path());
        std::ofstream stream(output);
        if (!stream)
            throw std::runtime_error("cannot write output file: " + output.string());

        const double arrivalTime = path.empty() ? std::numeric_limits<double>::quiet_NaN() : path.back().t;
        stream << std::boolalpha << std::fixed << std::setprecision(8);
        stream << "{\n"
               << "  \"planner\": \"ompl::geometric::STRRTstar\",\n"
               << "  \"ompl_version\": \"" << OMPL_VERSION << "\",\n"
               << "  \"status\": \"" << status.asString() << "\",\n"
               << "  \"solved\": " << static_cast<bool>(status) << ",\n"
               << "  \"planning_time_sec\": " << planningSeconds << ",\n"
               << "  \"arrival_time\": ";
        if (path.empty())
            stream << "null";
        else
            stream << arrivalTime;
        stream << ",\n"
               << "  \"spatial_length\": " << spatialLength << ",\n"
               << "  \"max_segment_speed\": " << maximumSegmentSpeed << ",\n"
               << "  \"v_max\": " << options.vMax << ",\n"
               << "  \"explored_states\": " << exploredStates << ",\n"
               << "  \"moving_obstacle_radius\": " << options.obstacleRadius << ",\n"
               << "  \"path\": [\n";
        for (std::size_t index = 0; index < path.size(); ++index)
        {
            const auto &point = path[index];
            stream << "    {\"x\": " << point.x << ", \"y\": " << point.y << ", \"t\": " << point.t << "}";
            if (index + 1 != path.size())
                stream << ',';
            stream << '\n';
        }
        stream << "  ]\n}\n";
    }
}

int main(int argc, char **argv)
{
    try
    {
        const Options options = parseOptions(argc, argv);
        ompl::RNG::setSeed(options.seed);

        auto positionSpace = std::make_shared<ob::RealVectorStateSpace>(2);
        ob::RealVectorBounds positionBounds(2);
        positionBounds.setLow(0.0);
        positionBounds.setHigh(1.0);
        positionSpace->setBounds(positionBounds);

        auto space = std::make_shared<ob::SpaceTimeStateSpace>(positionSpace, options.vMax, 0.35);
        space->setTimeBounds(0.0, options.maxTime);
        auto spaceInformation = std::make_shared<ob::SpaceInformation>(space);
        spaceInformation->setStateValidityChecker(DynamicObstacleValidityChecker(options.obstacleRadius));
        spaceInformation->setMotionValidator(std::make_shared<SpaceTimeMotionValidator>(spaceInformation));

        og::SimpleSetup setup(spaceInformation);
        ob::ScopedState<> start(space);
        start[0] = 0.05;
        start[1] = 0.05;
        ob::ScopedState<> goal(space);
        goal[0] = 0.95;
        goal[1] = 0.95;
        setup.setStartAndGoalStates(start, goal);
        setup.getProblemDefinition()->setOptimizationObjective(std::make_shared<ob::MinimizeArrivalTime>(spaceInformation));

        auto planner = std::make_shared<og::STRRTstar>(spaceInformation);
        planner->setRange(0.22);
        planner->setBatchSize(256);
        planner->setInitialTimeBoundFactor(1.6);
        planner->setTimeBoundFactorIncrease(1.5);
        planner->setOptimumApproxFactor(0.95);
        setup.setPlanner(planner);

        const auto started = std::chrono::steady_clock::now();
        const ob::PlannerStatus status = setup.solve(options.solveTime);
        const double planningSeconds =
            std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();

        ob::PlannerData plannerData(spaceInformation);
        planner->getPlannerData(plannerData);
        std::vector<PathPoint> path;
        double spatialLength = 0.0;
        double maximumSegmentSpeed = 0.0;
        if (status)
        {
            const auto &states = setup.getSolutionPath().getStates();
            path.reserve(states.size());
            for (const auto *state : states)
                path.push_back(pointFromState(state));

            for (std::size_t index = 1; index < path.size(); ++index)
            {
                const double dx = path[index].x - path[index - 1].x;
                const double dy = path[index].y - path[index - 1].y;
                const double distance = std::hypot(dx, dy);
                const double deltaTime = path[index].t - path[index - 1].t;
                spatialLength += distance;
                if (deltaTime > 0.0)
                    maximumSegmentSpeed = std::max(maximumSegmentSpeed, distance / deltaTime);
            }
        }

        std::cout << std::boolalpha << std::fixed << std::setprecision(6)
                  << "OMPL version: " << OMPL_VERSION << '\n'
                  << "Planner: ompl::geometric::STRRTstar\n"
                  << "Status: " << status.asString() << '\n'
                  << "Solved: " << static_cast<bool>(status) << '\n'
                  << "Planning time (s): " << planningSeconds << '\n'
                  << "Explored states: " << plannerData.numVertices() << '\n'
                  << "Path states: " << path.size() << '\n';
        if (!path.empty())
            std::cout << "Arrival time: " << path.back().t << '\n'
                      << "Spatial length: " << spatialLength << '\n'
                      << "Max segment speed: " << maximumSegmentSpeed << " (limit " << options.vMax << ")\n";

        if (!options.output.empty())
        {
            writeJson(options.output, options, status, planningSeconds, plannerData.numVertices(), path,
                      spatialLength, maximumSegmentSpeed);
            std::cout << "JSON output: " << std::filesystem::absolute(options.output).string() << '\n';
        }

        return status ? 0 : 2;
    }
    catch (const std::exception &error)
    {
        std::cerr << "error: " << error.what() << '\n';
        return 1;
    }
}
