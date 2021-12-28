from hpp.corbaserver.manipulation import ProblemSolver, Rule, Constraints, ConstraintGraph, ConstraintGraphFactory, Client
from hpp.gepetto.manipulation import ViewerFactory
from hpp.gepetto import PathPlayer
from hpp.corbaserver import loadServerPlugin
from hpp.quaternion import Quaternion
from hpp_idl.hpp import Error

from prl_hpp.tools.utils import compare_configurations, wd
from prl_hpp.tools.instate_planner import InStatePlanner

from prl_hpp.tools.hpp_robots import TargetRobotStrings
from prl_hpp.tools.hpp_robots import HppRobot

# import os
# loadServerPlugin ("corbaserver", os.environ.get('CONDA_PREFIX')+"/lib/hppPlugins/manipulation-corba.so")
loadServerPlugin ("corbaserver", "manipulation-corba.so")
Client ().problem.resetProblem ()

class Path:
    def __init__(self, id, corbaPath, jointList, targetFrames = []):
        self.id = id
        self.corbaPath = corbaPath
        self.jointList = jointList
        self.targetFrames = targetFrames

class Planner:
    """
    Wrap around HPP to simplify common plannig.
    """

    def __init__(self, robot):
        """
        Parameters
        ----------
            robot (Robot): Associated Robot class. (see robot.py)
        """
        self.robot = robot
        self.hpp_robot = HppRobot(robot.pin_robot_wrapper.model.name, "robot", robot.get_urdf_explicit(), robot.get_srdf_explicit())

        # Problem
        self.ps = ProblemSolver(self.hpp_robot)
        self.graph = self.hpp_robot.client.manipulation.graph

        # Viewer
        self.vf = ViewerFactory(self.ps)
        self.v = self.vf.createViewer()
        self.pp = PathPlayer(self.v)

        self.ps.setErrorThreshold (1e-3)
        self.ps.setMaxIterProjection (40)
        self.set_planning_timeout(10.0)

        # Time parametrization
        self.velocity_scale = 1.0
        self.acceleration_scale = 1.0

        self.lockJointConstraints = []

    def set_planning_timeout(self, timeout):
        """
        Set the maximum duration the planner has to find a solution

        Parameters
        ----------
            timeout (float): Timeout duration (in s.)
        """
        self.ps.setTimeOutPathPlanning(timeout)

    def lock_joints(self, jointNames, jointValues = None, constraintNames = None, lockName = "locked_joints"):
        """
        Lock joints that shouldn't be controlled.

        Create lock joints constraints and add it to the problem solver.

        Parameters
        ----------
            jointNames (str[]): Names of the joints to lock (with robotName prefix).

        Optionnals parameters:
        ----------------------
            jointValues (int[]): Values to lock the joints in. If unspecified lock the joint at the current values.
            constraintNames (str[]): Constraints name.
            lockName (str): Name of the lockJoint constraint.

        Returns
        -------
            constraintNames (str[]): name of created constraints.
        """
        # Generate constraints name
        if constraintNames == None:
            constraintNames = ["locked_" + jointName for jointName in jointNames]

        # get locked joint values from current configuration
        if jointValues == None:
            all_names = self.robot.get_joint_names()
            q_current = self.robot.get_meas_q()
            jointValues = []
            for j_name in jointNames:
                j_index = all_names.index(j_name)
                jointValues.append(q_current[j_index])

        # Create the constraints
        for i in range(len(jointNames)):
            self.ps.createLockedJoint(constraintNames[i], "robot/" + jointNames[i], [jointValues[i]])

        self.ps.addLockedJointConstraints(lockName, constraintNames)

        self.lockJointConstraints.extend(constraintNames)
        return constraintNames

    def make_gripper_approach(self, gripperName, position, orientation, approach_distance = 0.1, q_start = None, *, validate = True, do_not_plan = False):
        """
        Find a path that leads to the gripper at a certain pose with a certain approach direction.

        Parameters
        ----------
            gripperName (str): Names of the gripper (without robotName prefix), as defined in srdf.
            position (float[3]): Desired postion of the tool in world frame.
            orientation (float[3 or 4]): Desired orientation of the tool in world frame. (Can be euler angles or quaternions).

        Optionnals parameters:
        ----------------------
            approach_distance (float): Distance from which the gripper should make a quasi-straight to reach the goal pose.
            q_start (foat[]): The initial configuration of the robot. (If unspecified, the initial configuration is set to the current one.)
            validate (bool): If true, will check if the constraint graph generated is valid.
            do_not_plan (bool): If true, will not plan, but will generate the constraint graph with goal configurations etc. (useful to check if a goal pose is 'do-able'. Exception will be thrown if not)
        Returns
        -------
            path (Path): the path found

        Raises
        ------
            AssertionError: If the start configuration is not valid.
            AssertionError: If no valid goal configuration can be found.
            AssertionError: If validate is True and the graph is not found valid.
        """
        if q_start == None:
            q_start = self.robot.get_meas_q()

        orientation = Planner._convert_orientation(orientation)

        self._reset_problem()
        gripperFullname = "robot/" + gripperName
        gripperLink = self.robot.get_gripper_link(gripperName)

        # Create the approach object target
        self._create_target("target", approach_distance)

        # The configuration space is now bigger because of the configuration of the target
        q_start = q_start + (position + orientation)

        # Create the ConstraintGraph
        cg = self._create_simple_cg([gripperFullname], ['target'], [['target/handle']], validate)

        # Project the initial configuration in the initial node
        res_init, q_init, _ = cg.applyNodeConstraints("free", q_start)
        assert res_init, "Initial configuration is not valid"

        # Generate pair goal configuration (pre-graps, grasp)
        q_goals = []
        for _ in range(100):
            q = self.hpp_robot.shootRandomConfig()
            res_pre, q_pre, error_pre = cg.generateTargetConfig(gripperFullname + ' > target/handle | f_01', q_init, q)
            res_grasp, q_grasp, error_grasp = cg.generateTargetConfig(gripperFullname + ' > target/handle | f_12', q_pre, q_pre)
            if(res_pre and res_grasp):
                res_path, pathId, error_path = self.ps.directPath(q_pre, q_grasp, True)
                if res_path:
                    q_goals.append([q_pre, q_grasp])
                    self.ps.erasePath(pathId) # Erase the path as it's not needed anymore

        assert len(q_goals) > 0, "No goal configuration found"

        # Check if should plan or not
        if do_not_plan:
            return

        # Prepare solving in-state
        instatePlanner = InStatePlanner (self.ps)
        instatePlanner.setEdge(cg, "Loop | f")
        instatePlanner.optimizerTypes = [ "RandomShortcut" ]
        # instatePlanner.maxIterPathPlanning = 600
        instatePlanner.timeOutPathPlanning = self.ps.getTimeoutPlanning()

        # Solve the problem
        path = instatePlanner.computePath(q_init, [q_pre for q_pre, q_grasp in q_goals], resetRoadmap=True)

        # Add path to the problem
        pathId = self.ps.hppcorba.problem.addPath(path)

        # Find which pre-grasp configuration is at the end of the path
        q_end = path.end()
        q_end_grasp = None
        for q_pre, q_grasp in q_goals:
            if(compare_configurations(q_pre, q_end)):
                q_end_grasp = q_grasp
                break
        assert q_end_grasp != None, "Error while concatenating the last part of the path."

        # Add the pre-grasp to grap path
        self.ps.appendDirectPath(pathId, q_end_grasp, False)

        # Time parametrization of the path
        paramPathId = self._timeParametrizePath(pathId)
        paramPath = wd(self.ps.hppcorba.problem.getPath(paramPathId))

        # Delete the first path as it won't be used anymore
        path.deleteThis()

        # return path
        return Path(paramPathId, paramPath, self.robot.get_joint_names(), [gripperLink])

    def set_velocity_limit(self, scale):
        """
        Set the velocity limit for time parametrization of paths (relatively to the max joint velocity of the robot).

        Parameters
        ----------
            scale (float): Desired ratio of limit_velocity / max_velocity.
        """
        self.velocity_scale = scale

    def set_acceleration_limit(self, scale):
        """
        Set the acceleration limit for time parametrization of paths (relatively to the max joint acceleration of the robot).

        Parameters
        ----------
            scale (float): Desired ratio of limit_acceleration / max_acceleration.
        """
        self.acceleration_scale = scale

    def make_pick_and_place(self, gripperName, pose_pick, pose_place, approach_distance = 0.1, q_start = None, q_end = None, *, validate = True):
        """
        Find 3 paths to pick an object, place the object and go to end position.

        Parameters
        ----------
            gripperName (str): Names of the gripper (without robotName prefix), as defined in srdf.
            pose_pick ([float[3], float[3 or 4]]): Picking pose (position, orientation) of the tool in world frame. (orientation can be euler angles or quaternions).
            pose_place ([float[3], float[3 or 4]]): Placing pose (position, orientation) of the tool in world frame. (orientation can be euler angles or quaternions).

        Optionnals parameters:
        ----------------------
            approach_distance (float): Distance from which the gripper should make a quasi-straight line pick (and place) the object.
            q_start (foat[]): The initial configuration of the robot. (If unspecified, the initial configuration is set to the current one.)
            q_end (foat[]): The desired final configuration of the robot. (If unspecified, the final configuration will be the same as the initial one.)
            validate (bool): If true, will check if the constraint graph generated is valid.

        Returns
        -------
            paths (Path[3 (or 0)]): the path picking, placing and homing paths. If he list is empty, no path was found in the timeout duration.

        Raises
        ------
            AssertionError: If the start configuration is not valid.
            AssertionError: If the end configuration is not valid.
            AssertionError: If validate is True and the graph is not found valid.
        """
        if q_start == None:
            q_start = self.robot.get_meas_q()
        if q_end == None:
            q_end = q_start

        # Convert euler rotations into quaternions (if needed)
        pose_pick[1]  = Planner._convert_orientation(pose_pick[1])
        pose_place[1] = Planner._convert_orientation(pose_place[1])

        # Merge postion and orientation in one list
        pose_pick = sum(pose_pick, [])
        pose_place = sum(pose_place, [])

        self._reset_problem()
        gripperFullname = "robot/" + gripperName
        gripperLink = self.robot.get_gripper_link(gripperName)

        # Create the approach object target
        self._create_target("target", approach_distance)

        # The configuration space is now bigger because of the configuration of the target
        q_start = q_start + pose_pick
        q_end = q_end + pose_place

        # Create the ConstraintGraph
        cg = self._create_simple_cg([gripperFullname], ['target'], [['target/handle']], validate)

        # Project the initial configuration in the initial node
        res_init, q_init, _ = cg.applyNodeConstraints("free", q_start)
        assert res_init, "Initial configuration is not valid"
        self.ps.setInitialConfig(q_init)

        # Add goal config
        self.ps.resetGoalConfigs()
        res_goal, q_goal, _ = cg.applyNodeConstraints("free", q_end)
        assert res_goal, "End configuration is not valid"
        self.ps.addGoalConfig(q_goal)

        # Solve the problem
        success = self._safe_solve()
        if not success:
            return [] # No path found
        pathId = self.ps.numberPaths()-1

        # Optimize path
        self.ps.clearPathOptimizers()
        self.ps.addPathOptimizer("RandomShortcut")
        self.ps.optimizePath(pathId)
        pathId = self.ps.numberPaths()-1

        # Split the path
        wps = self.ps.getWaypoints(pathId)[0]
        pick_index = None
        place_index = None
        for i, q in enumerate(wps):
            target_pos = q[-7:]
            if(compare_configurations(target_pos, pose_pick)):
                pick_index = i
            if(not compare_configurations(target_pos, pose_place)):
                place_index = i+1

        # Create sub path
        pick_pathId = self._create_path(wps[:pick_index+1])
        place_pathId = self._create_path(wps[pick_index:place_index+1])
        home_pathId = self._create_path(wps[place_index:])

        # Time parametrization of the paths
        param_pick_pathId = self._timeParametrizePath(pick_pathId)
        param_place_pathId = self._timeParametrizePath(place_pathId)
        param_home_pathId = self._timeParametrizePath(home_pathId)

        # Extract paths
        param_pick_path = wd(self.ps.hppcorba.problem.getPath(param_pick_pathId))
        param_place_path = wd(self.ps.hppcorba.problem.getPath(param_place_pathId))
        param_home_path = wd(self.ps.hppcorba.problem.getPath(param_home_pathId))

        # return paths
        return  Path(param_pick_pathId, param_pick_path, self.robot.get_joint_names(), [gripperLink]), \
                Path(param_place_pathId, param_place_path, self.robot.get_joint_names(), [gripperLink]), \
                Path(param_home_pathId, param_home_path, self.robot.get_joint_names(), [gripperLink])


    def _create_target(self, targetName, clearance, bounds = [-2, 2]*3 + [-1, 1]*4):
        target = TargetRobotStrings(clearance)

        # Create the target object target
        self.v.loadRobotModelFromString(targetName, 'freeflyer', target.urdf, target.srdf)

        # Bound the pickTarget pose around the desired pose
        self.hpp_robot.setJointBounds (targetName + '/root_joint', bounds)

        # Re-create the viewer with the target
        self.v = self.vf.createViewer()
        self.pp = PathPlayer(self.v)

    def _reset_problem(self):
        try:
            self.graph.deleteGraph('graph')
        except Error:
            pass
        # self.ps.clearRoadmap()
        self.hpp_robot.__init__(self.robot.pin_robot_wrapper.model.name, "robot", self.robot.get_urdf_explicit(), self.robot.get_srdf_explicit())
        self.ps.resetGoalConfigs()

    def _timeParametrizePath(self, pathId):
        self.ps.clearPathOptimizers()
        self.ps.addPathOptimizer("SimpleTimeParameterization")

        self.ps.setParameter("SimpleTimeParameterization/safety", self.velocity_scale) # velocity limit factor
        self.ps.setParameter("SimpleTimeParameterization/order", 2)
        self.ps.setParameter("SimpleTimeParameterization/maxAcceleration", self.robot.MAX_JOINT_ACC * self.acceleration_scale)

        self.ps.optimizePath(pathId)
        return self.ps.numberPaths() -1

    def _convert_orientation(orientation):
        """
        Return the orientation as quaternions
        """
        if(len(orientation) == 3):
            quat = Quaternion()
            quat.fromRPY(*orientation)
            orientation = list(quat.toTuple())
        return orientation

    def _create_path(self, waypoints):
        _, pathId, _ = self.ps.directPath(waypoints[0], waypoints[1], False)
        for q in waypoints[2:]:
            self.ps.appendDirectPath(pathId, q, False)
        return pathId

    def _create_simple_cg(self, grippers, targets, targets_handles, validate):
        cg = ConstraintGraph (self.hpp_robot, 'graph')
        factory = ConstraintGraphFactory (cg)
        factory.setGrippers (grippers)
        factory.setObjects (targets, targets_handles, [[]*len(targets)])
        factory.setRules ([ Rule([".*"], [".*"], True), ])
        factory.generate ()
        cg.addConstraints (graph=True, constraints= Constraints(numConstraints = self.lockJointConstraints))
        cg.initialize()

        # Validate constraint graph
        if(validate):
            cproblem = self.ps.hppcorba.problem.getProblem()
            cgraph = cproblem.getConstraintGraph()
            cgraph.initialize()
            graphValidation = self.ps.client.manipulation.problem.createGraphValidation()
            assert graphValidation.validate(cgraph), graphValidation.str()

        return cg

    def _safe_solve(self):
        """
        Solve the problem and catch timeout exception
        """
        try:
            self.ps.solve()
        except Error as e:
            # print(e.msg)
            return False
        return True