import rospy
from sensor_msgs.msg import JointState
from prl_pinocchio.tools.utils import replace_path_to_absolute, compare_configurations
from prl_pinocchio.tools.observer import Observer
import pinocchio
import numpy


class Robot:
    """ User friendly Robot class that encapsulate the HppRobot and some ROS functionnality """
    def __init__(self, robot_description_param_prefix, joint_state_topic):
        """
        Parameters
        ----------
            robot_description_param_prefix (str): Prefix to get ros parameters 'urdf' and 'srdf' (that contains robot urdf and srdf strings).
            joint_state_topic (str): Topic name to get robot configuration.
        """
        # Get URDF/SRDF strings from ros parameters
        urdfString = rospy.get_param(robot_description_param_prefix + "/urdf")
        srdfString = rospy.get_param(robot_description_param_prefix + "/srdf")
        self._urdfStringExplicit = replace_path_to_absolute(urdfString)
        self._srdfStringExplicit = replace_path_to_absolute(srdfString)

        # Joint topic from ros
        self.joint_state_topic = joint_state_topic

        # Build pinocchio models and wrappers
        pin_model = pinocchio.buildModelFromXML(urdfString)
        pin_collision_model = pinocchio.buildGeomFromUrdfString(pin_model, self.get_urdf_explicit(), pinocchio.COLLISION)
        pin_visual_model    = pinocchio.buildGeomFromUrdfString(pin_model, self.get_urdf_explicit(), pinocchio.VISUAL)

        self.pin_robot_wrapper = pinocchio.RobotWrapper(pin_model, pin_collision_model, pin_visual_model)

        # # Check that for each ros joint the corresponding pinocchio model joint matches the name and index
        # ros_joint_names = rospy.wait_for_message(self.joint_state_topic, JointState).name
        # assert len(ros_joint_names) == len(self.pin_model.names[1:]), \
        #        F"Error number of joints differ between ROS and pinocchio model : {len(ros_joint_names)} != {len(self.pin_model.names[1:])}"
        #        # Pinocchio joints starts at 1 to take into account the universe joint that is not in ros.
        # for index, name in enumerate(ros_joint_names):
        #     assert self.pin_model.names[index+1] == name, \
        #            F"Error while matching ROS and pinocchio model joints at index {index} : joint name {self.pin_model.names[index+1]} differs from {name}."
        #            # Pinocchio index starts at +1 to take into account the universe joint.

        # Make a lookup table to change between ros joint index and pinocchio joint index (using joints names)
        # The input/output of this class will always be according to pinocchio joint order
        self._joint_state_obs = Observer(joint_state_topic, JointState)
        rospy.loginfo(F"Wait for a JointState message on {self.joint_state_topic}...")

        # Prepare lookup table to re-arrange q, v, a from ros to pinocchio format, etc..
        ros_joint_names = list(self._joint_state_obs.get_last_msg().name)
        self._q_pin_to_ros, self._q_ros_to_pin, self._v_pin_to_ros, self._v_ros_to_pin = self.create_dof_lookup(ros_joint_names)

    def get_urdf_explicit(self):
        return self._urdfStringExplicit

    def get_srdf_explicit(self):
        return self._srdfStringExplicit

    def get_meas_q(self, raw=False):
        """
        Get the current configuration of the robot.

        Read it from the ros 'joint_state_topic' topic.

        Optionnals parameters:
        ----------------------
            raw (bool): If not set to True, the configuration will be adjusted to fit in the joints bounds.

        Returns
        -------
            q (float[]): the configuration.

        Raises
        ------
            AssertionError: If the adjusted configuration deviates too much from the original one.
        """
        q, _, _ = self.get_meas_qvtau(raw)
        return q

    def get_meas_qvtau(self, raw=False):
        """
        Get the current position, velocity and effort of everyjoint of the robot.

        Read it from the ros 'joint_state_topic' topic.

        Optionnals parameters:
        ----------------------
            raw (bool): If not set to True, the configuration will be adjusted to fit in the joints bounds.

        Returns
        -------
            q (float[]): the configuration.
            v (float[]): the velocities.
            tau (float[]): the efforts.

        Raises
        ------
            AssertionError: If the adjusted configuration deviates too much from the original one.
        """
        joints_state = self._joint_state_obs.get_last_msg()
        q = list(joints_state.position)
        v = list(joints_state.velocity)
        tau = list(joints_state.effort)

        q, v, tau = self._rearrange_ros_to_pin(q=q, v=v, tau=tau)

        if not raw:
            for i, pos in enumerate(q):
                pos = max(pos, self.pin_robot_wrapper.model.lowerPositionLimit[i])
                pos = min(pos, self.pin_robot_wrapper.model.upperPositionLimit[i])
                assert abs(pos - q[i]) < 1e-3, F"Joint {i} way out of bounds : {self.pin_robot_wrapper.pin_model.names[i+1]}"
                q[i] = pos

        return q, v, tau

    def get_meas_pose(self, jointName, q=None):
        """
        Get the current 6D pose of a joint.

        Compute the forward kinematic from the configuration.

        Parameters:
        ----------------------
            jointName (str): Name of the joint to read the pose of.

        Optionnals parameters:
        ----------------------
            q (float[]): Configuration of the robot. If None, the current configuration will be read from 'joint_state_topic'.

        Returns
        -------
            xyz_quat (float[3+4]): position and orientation (as a quaternion) coordinates concatenanted.

        Raises
        ------
            AssertionError: If jointName is not in the robot model.
        """
        if(q==None):
            q = self.get_meas_q()

        q = numpy.matrix(q).T

        self.pin_robot_wrapper.forwardKinematics(q)

        frame_index = self.pin_robot_wrapper.model.getJointId(jointName)
        assert frame_index < len(self.pin_robot_wrapper.data.oMi), "Joint name not found in robot model : " + jointName

        oMf = self.pin_robot_wrapper.data.oMi[frame_index]
        xyz_quat = pinocchio.SE3ToXYZQUATtuple(oMf)
        return xyz_quat

    def get_joint_names(self):
        """
        Get the name of every 'actuated' joints as it's defined in pinocchio from the urdf model.

        Returns
        -------
            jointNames (str[]): List of the names
        """
        return list(self.pin_robot_wrapper.model.names[1:])

    def is_at_config(self, q, threshold=0.1):
        """
        Check wether the robot is at a certain configuration.

        Compare every joint position to the desired position and check if it is under a certain tolerance.

        Parameters
        ----------
            q (float[]): The desired configuration of the robot.

        Optionnals parameters:
        ----------------------
            threshold (foat): Error tolerance for each joint.

        Returns
        -------
            is_at_config (bool): True if the robot is at the desired configuration.

        Raises
        ------
            AssertionError: If the desired configuration size doesn't match.
        """
        q_curr = self.get_meas_q()
        return compare_configurations(q, q_curr, threshold)

    def create_visualizer(self):
        from pinocchio.visualize import RVizVisualizer
        self.pin_robot_wrapper.setVisualizer(RVizVisualizer())
        self.pin_robot_wrapper.initViewer(loadModel=True, initRosNode=False)

    def display(self, q):
        self.pin_robot_wrapper.display(q)


    def create_dof_lookup(self, new_joint_list):
        pin_model = self.pin_robot_wrapper.model
        pin_joint_names = self.get_joint_names()

        # enumerate all the DoF and group them by joint
        index = 0
        q_pin_to_pin = []
        for joint in pin_model.joints:
            joint_indexes = []
            for i in range(joint.nq):
                joint_indexes.append(index)
                index+=1
            q_pin_to_pin.append(joint_indexes)
        # rearrange joints
        q_new_to_pin = []
        for new_index, name in enumerate(new_joint_list):
            pin_index = pin_joint_names.index(name)
            q_new_to_pin.append(q_pin_to_pin[pin_index])
        # Flatten the list of list into a simple list
        res_q_new_to_pin = []
        for indexes in q_new_to_pin:
            res_q_new_to_pin.extend(indexes)
        # Take the inverse of the bijection
        res_q_pin_to_new = []
        for pin_index in range(len(res_q_new_to_pin)):
            new_index = res_q_new_to_pin.index(pin_index)
            res_q_pin_to_new.append(new_index)

        # re-do all of the previous for v (instead of q)
        # enumerate all the DoF and group them by joint
        index = 0
        v_pin_to_pin = []
        for joint in pin_model.joints:
            joint_indexes = []
            for i in range(joint.nv):
                joint_indexes.append(index)
                index+=1
            v_pin_to_pin.append(joint_indexes)
        # rearrange joints
        v_new_to_pin = []
        for new_index, name in enumerate(new_joint_list):
            pin_index = pin_joint_names.index(name)
            v_new_to_pin.append(v_pin_to_pin[pin_index])
        # Flatten the list of list into a simple list
        res_v_new_to_pin = []
        for indexes in v_new_to_pin:
            res_v_new_to_pin.extend(indexes)
        # Take the inverse of the bijection
        res_v_pin_to_new = []
        for pin_index in range(len(res_v_new_to_pin)):
            new_index = res_v_new_to_pin.index(pin_index)
            res_v_pin_to_new.append(new_index)

        return res_q_pin_to_new, res_q_new_to_pin, res_v_pin_to_new, res_v_new_to_pin 

    def _rearrange_ros_to_pin(self, q=None, v=None, tau=None):
        res  = []

        # Remark: to convert a q (from ros) to a q (from pin), it use q_pin_to_ros because we want the ros index for each pin index (1, 2, 3, 4, ...)
        # So then it picks the appropriate ros coordinate and arrange them in order.
        if q != None:
            q_res = [q[self._q_pin_to_ros[i]] for i in range(len(q))]
            res.append(q_res)

        if v != None:
            v_res = [v[self._v_pin_to_ros[i]] for i in range(len(v))]
            res.append(v_res)

        if tau != None:
            tau_res = [tau[self._v_pin_to_ros[i]] for i in range(len(tau))]
            res.append(tau_res)

        return res